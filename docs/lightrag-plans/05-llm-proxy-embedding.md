# 05 - LLM Proxy Recovery, Embedding Adapter & Reranker Integration

> 最后更新：2026-04-22
> 状态：✅ 方案已讨论确认

## Overview

This document designs three interdependent layers required for LightRAG integration:

1. **LLM Proxy** -- Recover the deleted `page_agent_proxy.py`, generalize it as an OpenAI-compatible proxy at `/llm/v1/`, and verify it satisfies LightRAG's `AsyncOpenAI` client expectations.
2. **Embedding Adapter** -- Design the migration from the current 384-dim `paraphrase-multilingual-MiniLM-L12-v2` to a 1024-dim `BAAI/bge-m3` model that LightRAG recommends, including a re-indexing strategy for all existing vector data.
3. **Reranker Integration** -- Design how to integrate `BAAI/bge-reranker-v2-m3` as a local reranker, either by exposing it through the proxy or by providing a Python callable that satisfies LightRAG's `rerank_model_func` interface.

### 讨论确认的决策

| # | 决策 | 理由 |
|---|------|------|
| D1 | Embedding/Reranker 模型可插拔 | 配置文件层切换，不改代码；用户电脑性能好时可换更好模型 |
| D2 | 插拔方式写入系统管理手册 | 主Agent看手册后可帮用户切换模型 |

---

## Task 1: LLM Proxy Recovery & Generalization

### 1.1 Source Recovery

The file `niu_api/page_agent_proxy.py` was deleted in commit `fc8ec3e` ("chore: remove old Page Agent architecture + prepare for browser automation"). The last good version exists at commit `d656740`.

**Recovery command:**
```bash
git show d656740:niu_api/page_agent_proxy.py > niu_api/llm_proxy.py
```

The recovered file provides:
- OpenAI-compatible request/response Pydantic models (`OpenAIChatRequest`, `OpenAIChatResponse`, etc.)
- Format converters: `openai_to_litellm_messages()`, `openai_to_litellm_tools()`, `litellm_to_openai_response()`
- LLM invocation via `LiteLLMSession.chat()` with generator consumption
- Endpoints: `POST /chat/completions`, `GET /models`, `GET /health`
- Config reader: `get_llm_config()` reads from `config/user-config.json`

### 1.2 What Must Change

The recovered proxy is Page-Agent-specific. To serve as a general-purpose LLM proxy (usable by LightRAG, browser extensions, and any OpenAI client), the following changes are required:

#### A. Rename and Repath

| Old | New |
|-----|-----|
| `niu_api/page_agent_proxy.py` | `niu_api/llm_proxy.py` |
| `APIRouter(prefix="/proxy/v1")` | `APIRouter(prefix="/llm/v1")` |
| Tags: `"page-agent-proxy"` | Tags: `"llm-proxy"` |
| Log prefix: `[Page-Agent Proxy]` | `[LLM Proxy]` |

#### B. Add Embeddings Endpoint

LightRAG calls `openai_async_client.embeddings.create()` which hits `POST /v1/embeddings`. The current proxy has no embeddings endpoint.

**New endpoint:**
```python
class OpenAIEmbeddingRequest(BaseModel):
    model: str
    input: Union[str, List[str]]  # single text or batch
    encoding_format: Optional[str] = "float"  # "float" or "base64"
    dimensions: Optional[int] = None  # for dimension reduction

class OpenAIEmbeddingResponse(BaseModel):
    id: str
    object: str = "list"
    created: int
    model: str
    data: List[Dict[str, Any]]  # [{"object":"embedding","embedding":[...],"index":0}]
    usage: Dict[str, int]  # {"prompt_tokens":N,"total_tokens":N}
```

**Implementation** calls `niu_api.internal.embedding.batch_encode()` and returns OpenAI-format response.

#### C. Support Non-Streaming Responses (Critical for LightRAG)

LightRAG's `openai_complete_if_cache()` calls `client.chat.completions.create()` and expects a **non-streaming** `ChatCompletion` object with `.choices[0].message.content`. The current proxy always consumes the generator and builds a synthetic response, which works but loses the native OpenAI response structure.

**Changes needed:**
1. Add `stream` parameter to `OpenAIChatRequest` (default `False`)
2. When `stream=False` (the LightRAG default): call the LLM synchronously and return a complete `ChatCompletion` response
3. When `stream=True`: support SSE streaming for browser extension use cases

The current generator-consumption approach already produces a complete response, so the non-streaming path is already functional. The key fix is to ensure the response format exactly matches what `openai.AsyncOpenAI` client would return, including:
- `response.id` with `chatcmpl-` prefix
- `response.object` = `"chat.completion"`
- `response.created` as Unix timestamp
- `response.choices[0].finish_reason` as `"stop"` or `"tool_calls"`
- `response.usage` with accurate token counts

#### D. Support `response_format` (JSON Mode)

LightRAG uses `response_format` for structured output during keyword extraction:
```python
# In openai_complete_if_cache():
if keyword_extraction:
    kwargs["response_format"] = GPTKeywordExtractionFormat
```

**Add to `OpenAIChatRequest`:**
```python
response_format: Optional[Dict[str, Any]] = None  # {"type":"json_object"} or structured schema
```

This gets passed through to the LLM call. LiteLLM supports `response_format` natively.

#### E. Remove Page-Agent Specifics

- Remove Page-Agent references in docstrings and comments
- Remove `parallel_tool_calls` from the request model (LightRAG never sends this; keep it optional for forward compat)
- The `tools`/`tool_choice` fields remain -- LightRAG doesn't use them, but they are harmless and needed for browser extension use cases

### 1.3 LightRAG Compatibility Verification

LightRAG's `openai.py` uses `openai.AsyncOpenAI` with these API calls:

| LightRAG Call | API Endpoint | Proxy Support |
|---------------|-------------|---------------|
| `client.chat.completions.create(model, messages, ...)` | `POST /v1/chat/completions` | **Yes** (existing) |
| `client.chat.completions.parse(model, messages, response_format=...)` | `POST /v1/chat/completions` (with `response_format`) | **Needs addition** (1.2.D) |
| `client.embeddings.create(model, input, ...)` | `POST /v1/embeddings` | **Needs addition** (1.2.B) |

**LightRAG does NOT use:**
- Streaming (for entity extraction / query) -- it only uses streaming for query responses, and we can start with non-streaming
- `tool_calls` / `function_calling` -- LightRAG uses structured output (`response_format`) instead
- `parallel_tool_calls`

**LightRAG DOES use:**
- `system_prompt` + `history_messages` + `prompt` -- assembled into the messages array
- `keyword_extraction` mode with `response_format` -- must pass through
- COT (`reasoning_content`) -- the proxy should pass through any reasoning content from the underlying LLM
- Retry with `tenacity` (3 attempts, exponential backoff) -- the proxy itself should NOT retry; let the LightRAG client handle retries

### 1.4 Configuration for LightRAG

LightRAG is configured to point at our proxy by setting these environment variables or constructor params:

```python
# LightRAG initialization pointing at our proxy
rag = LightRAG(
    llm_model_func=openai_complete,  # or partial(openai_complete_if_cache, ...)
    llm_model_name="proxy-model",  # any name, proxy ignores and uses config
    embedding_func=EmbeddingFunc(
        embedding_dim=1024,  # bge-m3 dimension
        max_token_size=8192,
        func=partial(openai_embed, model="bge-m3", base_url="http://localhost:9876/llm/v1", api_key="not-needed"),
    ),
)
```

Or equivalently via environment variables:
```bash
LLM_BINDING=openai
LLM_BINDING_HOST=http://localhost:9876/llm/v1
LLM_BINDING_API_KEY=not-needed  # proxy reads from user-config.json
EMBEDDING_BINDING=openai
EMBEDDING_BINDING_HOST=http://localhost:9876/llm/v1
EMBEDDING_BINDING_API_KEY=not-needed
EMBEDDING_MODEL=bge-m3
EMBEDDING_DIM=1024
```

### 1.5 Route Registration

In `niu_api/__main__.py`:
```python
from niu_api.llm_proxy import router as llm_proxy_router
app.include_router(llm_proxy_router)  # LLM Proxy API (/llm/v1/*)
```

### 1.6 File Structure

```
niu_api/
  llm_proxy.py          # General-purpose OpenAI-compatible proxy
    POST /llm/v1/chat/completions
    POST /llm/v1/embeddings
    GET  /llm/v1/models
    GET  /llm/v1/health
```

---

## Task 2: Embedding Adapter

### 2.1 Current State

**Model:** `paraphrase-multilingual-MiniLM-L12-v2` (SentenceTransformer)
- **Dimension:** 384
- **Location:** `models/paraphrase-multilingual-MiniLM-L12-v2/`
- **Size:** ~480 MB on disk
- **Loading:** `niu_api/internal/embedding.py` -> `SentenceTransformer(model_path)`
- **Used by:**
  - `agent/vector_search.py` (VectorSearchAdapter._get_embedding)
  - `mcp-servers/vector-store/src/niu_vector_store/__init__.py` (via niu_api.internal.embedding)
  - `mcp-servers/kg-server/` (for knowledge graph vector operations)
  - `agent/injector/sync.py` (skill synchronization)

**Embedding data stored in:**
- `vectors.db` (SQLite) -- `documents.embedding` BLOB column, raw float32 bytes
- All existing vectors are 384-dimensional

### 2.2 Candidate: BAAI/bge-m3

**Why bge-m3:**
- LightRAG's recommended embedding model
- 1024 dimensions (dense) -- much richer representation
- Multilingual (strong Chinese + English support)
- Supports dense + sparse + ColBERT retrieval modes
- 8192 max token size (vs. current 128-256 effective)
- Well-tested with LightRAG's `NanoVectorDBStorage`

**Model specs:**
- **Dimension:** 1024
- **Max tokens:** 8192
- **Size:** ~570 MB on disk (FP32), ~285 MB (FP16)
- **Framework:** `sentence-transformers` compatible (same loading code)

### 2.3 Adapter Design

**Option A: Local SentenceTransformer with bge-m3** (RECOMMENDED)

```python
# niu_api/internal/embedding.py (modified)
_model = None  # module-level singleton

def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        models_dir = get_models_dir()
        device = get_device()

        bge_m3_model = models_dir / "bge-m3"
        legacy_model = models_dir / "paraphrase-multilingual-MiniLM-L12-v2"

        if bge_m3_model.exists():
            _model = SentenceTransformer(str(bge_m3_model))
        elif legacy_model.exists():
            _model = SentenceTransformer(str(legacy_model))
            logger.warning("Using legacy 384-dim model; vector DB may need re-indexing")
        else:
            _model = SentenceTransformer("BAAI/bge-m3")
            _model.save(str(bge_m3_model))

        _model = _model.to(device)
    return _model

def get_embedding_dim() -> int:
    """Return the dimension of the current embedding model."""
    model = get_model()
    return model.get_sentence_embedding_dimension()
```

**Pros:** No network overhead, same architecture as current, reuse existing loading code.
**Cons:** Higher memory (~570 MB vs ~480 MB), but acceptable.

**Option B: Proxy through API (like LLM proxy)**

LightRAG would call `POST /llm/v1/embeddings` which calls `niu_api.internal.embedding.batch_encode()`.

**Pros:** Single model instance shared across all callers.
**Cons:** Adds HTTP latency (~5ms per call). But since LightRAG uses `AsyncOpenAI`, this is natural.

**Option C: LightRAG's built-in `openai_embed`**

Let LightRAG call an external OpenAI-compatible embeddings API directly.

**Pros:** No custom code.
**Cons:** Requires the proxy endpoint (Option B) anyway, and doesn't help the existing ai-bot vector search.

**Decision: Option A for local use + Option B endpoint in proxy for LightRAG.**

The embedding model is shared: both the proxy endpoint and the in-process `embedding.encode()` use the same `get_model()` singleton. This avoids loading two copies.

### 2.4 Proxy Embeddings Endpoint Implementation

```python
# In niu_api/llm_proxy.py

@router.post("/embeddings")
async def create_embeddings(request: OpenAIEmbeddingRequest) -> OpenAIEmbeddingResponse:
    """OpenAI-compatible embeddings endpoint."""
    from niu_api.internal.embedding import batch_encode, get_model
    import time, uuid

    # Normalize input to list
    texts = request.input if isinstance(request.input, list) else [request.input]

    # Get embeddings using shared model
    embeddings = batch_encode(texts)

    # Apply dimension reduction if requested
    if request.dimensions and request.dimensions < len(embeddings[0]):
        # Truncate vectors (Matryoshka-style dimension reduction)
        embeddings = [e[:request.dimensions] for e in embeddings]

    # Format response
    data = [
        {
            "object": "embedding",
            "embedding": emb,
            "index": idx,
        }
        for idx, emb in enumerate(embeddings)
    ]

    return OpenAIEmbeddingResponse(
        id=f"embd-{uuid.uuid4().hex[:8]}",
        created=int(time.time()),
        model=request.model,
        data=data,
        usage={
            "prompt_tokens": sum(len(t.split()) for t in texts),
            "total_tokens": sum(len(t.split()) for t in texts),
        }
    )
```

### 2.5 Model Migration Impact: Dimension Change 384 -> 1024

**Critical issue:** All existing vector data in `vectors.db` uses 384-dimensional embeddings. Changing to 1024 dimensions makes ALL existing vectors invalid -- cosine similarity between a 384-dim and 1024-dim vector is undefined.

**Affected data:**
- `vectors.db` -- all rows in `documents` table with non-NULL `embedding` BLOB
- Estimated: thousands of rows (L0/L1/L2 knowledge, MCP tool descriptions, interaction habits, query patterns)

**Re-indexing strategy:**

#### Phase 1: Dual-Model Support (Transition Period)

```python
# niu_api/internal/embedding.py

_LEGACY_MODEL = None  # 384-dim, loaded on demand only
_CURRENT_MODEL = None  # 1024-dim, primary

def get_legacy_model():
    """Load legacy 384-dim model only for re-indexing."""
    global _LEGACY_MODEL
    if _LEGACY_MODEL is None:
        from sentence_transformers import SentenceTransformer
        models_dir = get_models_dir()
        legacy_path = models_dir / "paraphrase-multilingual-MiniLM-L12-v2"
        if legacy_path.exists():
            _LEGACY_MODEL = SentenceTransformer(str(legacy_path))
    return _LEGACY_MODEL
```

#### Phase 2: Re-Indexing Script

```python
# scripts/reindex_vectors.py
"""
Re-index all vectors in vectors.db from 384-dim to 1024-dim.

Steps:
1. Load bge-m3 model
2. For each row in documents table:
   a. Read content text
   b. Compute new 1024-dim embedding
   c. Update embedding BLOB in-place
3. Verify: spot-check a few vectors have correct dimension
"""
import sqlite3
import numpy as np
from niu_api.internal.embedding import get_model

def reindex(db_path: str, batch_size: int = 100):
    model = get_model()  # bge-m3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    # Count rows
    total = conn.execute("SELECT COUNT(*) FROM documents WHERE content IS NOT NULL").fetchone()[0]
    print(f"Re-indexing {total} documents...")

    offset = 0
    while offset < total:
        rows = conn.execute(
            "SELECT id, content FROM documents WHERE content IS NOT NULL LIMIT ? OFFSET ?",
            (batch_size, offset)
        ).fetchall()

        if not rows:
            break

        # Batch encode
        texts = [row[1] for row in rows]
        embeddings = model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)

        # Update in transaction
        conn.execute("BEGIN IMMEDIATE")
        for (doc_id, _), emb in zip(rows, embeddings):
            emb_blob = emb.astype(np.float32).tobytes()
            conn.execute("UPDATE documents SET embedding = ? WHERE id = ?", (emb_blob, doc_id))
        conn.execute("COMMIT")

        offset += batch_size
        print(f"  Re-indexed {min(offset, total)}/{total}")

    conn.close()
    print("Done!")
```

#### Phase 3: Migration Sequence

1. **Pre-check:** Verify bge-m3 model is downloaded (`models/bge-m3/`)
2. **Backup:** Copy `vectors.db` to `vectors.db.384dim.bak`
3. **Re-index:** Run `scripts/reindex_vectors.py`
4. **Verify:** Spot-check 10 random vectors have 1024 dimensions
5. **Update config:** Change default embedding model to bge-m3
6. **Clean up:** After 7 days of stable operation, delete backup

#### Can we run both models during transition?

**Yes, but at memory cost:** Loading both models simultaneously requires ~480 MB + ~570 MB = ~1.05 GB RAM. This is feasible on a development machine but not recommended for production. Instead, the re-indexing script loads bge-m3 exclusively, does the migration, then the app starts with bge-m3 only.

**For the KG server (LightRAG's own vector DB):** LightRAG stores its own embeddings separately in `rag_storage/`. This is independent of our `vectors.db`. LightRAG will create new 1024-dim vectors from scratch when documents are inserted via `ainsert()`. No migration needed for LightRAG's own data -- only for our existing ai-bot vectors.

### 2.6 Embedding Dimension Awareness

Code that currently assumes 384 dimensions (via `np.frombuffer(embedding_blob, dtype=np.float32)`) does NOT hardcode 384 anywhere -- it infers dimension from the blob length. This is already correct and requires no changes for 1024-dim vectors.

**Verification points:**
- `agent/vector_search.py`: Uses `np.frombuffer(blob, dtype=np.float32)` -- dimension agnostic
- `mcp-servers/vector-store/`: Same pattern -- dimension agnostic
- No code references `384` as a constant

---

## Task 3: Reranker Integration

### 3.1 LightRAG's Reranker Architecture

LightRAG supports three reranker backends via `--rerank-binding`:

| Binding | Model | API |
|---------|-------|-----|
| `cohere` | `rerank-v3.5` | `POST https://api.cohere.com/v2/rerank` |
| `jina` | `jina-reranker-v2-base-multilingual` | `POST https://api.jina.ai/v1/rerank` |
| `aliyun` | `gte-rerank-v2` | Aliyun DashScope API |

All three are **cloud API rerankers** that follow the same interface:

```python
async def rerank_func(query: str, documents: list[str], top_n: int = None) -> list[dict]:
    """Returns [{"index": int, "relevance_score": float}, ...]"""
```

LightRAG applies reranking in `operate.py` during `kg_query()`:
1. Vector search retrieves `chunk_top_k` text chunks
2. If `enable_rerank=True` and `rerank_model_func` is set, call `rerank_model_func(query, chunk_texts, top_n=chunk_top_k)`
3. Filter results by `min_rerank_score` threshold
4. Use reranked chunks for LLM context

### 3.2 Candidate: BAAI/bge-reranker-v2-m3

**Why bge-reranker-v2-m3:**
- Matches bge-m3 embedding model (same BAAI family)
- Multilingual (Chinese + English)
- Cross-encoder architecture (higher quality than bi-encoder)
- Can run locally via `sentence-transformers` or `FlagEmbedding`

**Model specs:**
- **Size:** ~560 MB
- **Max query length:** 512 tokens
- **Max document length:** 512 tokens
- **Framework:** `FlagEmbedding` (official) or `sentence-transformers` compatible

### 3.3 Reranker Design Options

#### Option A: Local CrossEncoder Model (RECOMMENDED)

Load `BAAI/bge-reranker-v2-m3` as a `CrossEncoder` from `sentence-transformers`:

```python
# niu_api/internal/reranker.py

_reranker = None

def get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder
        models_dir = get_models_dir()
        reranker_path = models_dir / "bge-reranker-v2-m3"

        if reranker_path.exists():
            _reranker = CrossEncoder(str(reranker_path))
        else:
            _reranker = CrossEncoder("BAAI/bge-reranker-v2-m3")
            _reranker.save(str(reranker_path))

    return _reranker

def rerank(query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
    """
    Rerank documents against query using local CrossEncoder.

    Returns:
        [{"index": int, "relevance_score": float}, ...] sorted by score descending.
    """
    model = get_reranker()

    # Create query-document pairs
    pairs = [[query, doc] for doc in documents]

    # Score all pairs
    scores = model.predict(pairs)

    # Build results
    results = [
        {"index": idx, "relevance_score": float(score)}
        for idx, score in enumerate(scores)
    ]

    # Sort by score descending
    results.sort(key=lambda x: x["relevance_score"], reverse=True)

    # Apply top_n limit
    if top_n is not None:
        results = results[:top_n]

    return results
```

**Pros:**
- No API calls, no network latency
- No API key needed
- No rate limits
- Data stays local (privacy)
- Fast for small document sets (<100 chunks)

**Cons:**
- ~560 MB additional memory
- Slower than API for large document sets (GPU helps)
- Must handle 512-token truncation

#### Option B: Rerank Proxy Endpoint

Expose reranking as a `/v1/rerank` endpoint compatible with Cohere/Jina format:

```python
# In niu_api/llm_proxy.py

@router.post("/rerank")
async def rerank_endpoint(request: RerankRequest):
    from niu_api.internal.reranker import rerank
    results = rerank(request.query, request.documents, request.top_n)
    return {"results": results}
```

Then configure LightRAG with `--rerank-binding=cohere` pointing at our proxy URL.

**Pros:** Reuses LightRAG's built-in `cohere_rerank` with custom base_url.
**Cons:** Adds HTTP round-trip; LightRAG's `cohere_rerank` expects a specific response format.

#### Option C: Python Callable (Direct)

Provide a Python async callable that satisfies LightRAG's `rerank_model_func` interface:

```python
async def local_rerank_func(query: str, documents: list, top_n: int = None, extra_body: dict = None):
    """LightRAG-compatible rerank function using local model."""
    from niu_api.internal.reranker import rerank
    return rerank(query, documents, top_n)

# Pass to LightRAG:
rag = LightRAG(
    rerank_model_func=local_rerank_func,
    ...
)
```

**Pros:** Zero overhead, direct function call.
**Cons:** Requires importing niu_api into the LightRAG process (tight coupling).

**Decision: Option A (local model) + Option C (direct callable) for LightRAG integration.**

Option A gives us the local model. Option C provides the cleanest integration with LightRAG since we're running LightRAG in-process. The rerank callable is a simple wrapper.

### 3.4 Reranker Memory/CPU Impact

| Component | Memory | Notes |
|-----------|--------|-------|
| bge-m3 (embedding) | ~570 MB | Always loaded |
| bge-reranker-v2-m3 | ~560 MB | Lazy-loaded on first query |
| **Total additional** | **~560 MB** | Reranker only |

**Lazy loading strategy:**
- Reranker is NOT loaded at startup
- Loaded on first `rerank()` call
- Unloaded after 10 minutes idle (configurable)
- This avoids memory pressure when no queries are happening

**CPU/GPU:**
- On CPU: reranking 20 documents takes ~200ms
- On GPU: reranking 20 documents takes ~10ms
- Batching helps: single `model.predict()` call for all pairs

### 3.5 512-Token Truncation

`bge-reranker-v2-m3` has a 512-token limit per document. LightRAG's `rerank.py` already provides `chunk_documents_for_rerank()` that splits long documents into overlapping 480-token chunks (with 32-token overlap). We should reuse this:

```python
def rerank(query: str, documents: list[str], top_n: int | None = None) -> list[dict]:
    from lightrag.rerank import chunk_documents_for_rerank, aggregate_chunk_scores

    # Chunk long documents
    chunked_docs, doc_indices = chunk_documents_for_rerank(
        documents, max_tokens=480, overlap_tokens=32
    )

    # Score chunked documents
    model = get_reranker()
    pairs = [[query, doc] for doc in chunked_docs]
    scores = model.predict(pairs)

    chunk_results = [
        {"index": idx, "relevance_score": float(score)}
        for idx, score in enumerate(scores)
    ]

    # Aggregate back to original documents
    results = aggregate_chunk_scores(
        chunk_results, doc_indices, len(documents), aggregation="max"
    )

    if top_n is not None:
        results = results[:top_n]

    return results
```

### 3.6 Reranker File Structure

```
niu_api/
  internal/
    reranker.py          # Local CrossEncoder reranker
      get_reranker()     # Lazy-load model singleton
      rerank()           # Synchronous rerank function
      preload()          # Optional startup preload
  llm_proxy.py           # (optional /rerank endpoint for API access)
```

---

## Integration Summary

### Data Flow

```
LightRAG (Python process, same as ai-bot)
  |
  |-- ainsert() / aquery()
  |     |
  |     |-- LLM calls (entity extraction, query answering)
  |     |     |
  |     |     +--> AsyncOpenAI(base_url="http://localhost:9876/llm/v1")
  |     |           |
  |     |           +--> niu_api/llm_proxy.py
  |     |                 |
  |     |                 +--> LiteLLMSession -> user-config.json LLM
  |     |
  |     |-- Embedding calls (vectorize entities, chunks)
  |     |     |
  |     |     +--> AsyncOpenAI(base_url="http://localhost:9876/llm/v1")
  |     |           |
  |     |           +--> niu_api/llm_proxy.py (/embeddings)
  |     |                 |
  |     |                 +--> niu_api/internal/embedding.py (bge-m3, 1024-dim)
  |     |
  |     |-- Rerank calls (after vector search, before LLM context)
  |           |
  |           +--> local_rerank_func (Python callable)
  |                 |
  |                 +--> niu_api/internal/reranker.py (bge-reranker-v2-m3)
```

### Memory Budget (After Full Integration)

| Component | Memory | Loaded |
|-----------|--------|--------|
| Python runtime + FastAPI | ~100 MB | Always |
| LiteLLM (no model, API calls) | ~50 MB | Always |
| bge-m3 (SentenceTransformer) | ~570 MB | Always (used by both proxy and local search) |
| bge-reranker-v2-m3 (CrossEncoder) | ~560 MB | Lazy (on first query, idle timeout 10min) |
| InsightFace (photo server) | ~326 MB | Lazy (idle timeout 5min) |
| **Total (steady state)** | **~720 MB** | Without reranker/photo |
| **Total (peak)** | **~1.6 GB** | All models loaded |

### Implementation Order

1. **Phase 1: LLM Proxy** (no dependencies)
   - Recover `page_agent_proxy.py` as `llm_proxy.py`
   - Rename prefix, update logs, add `/embeddings` endpoint
   - Add `response_format` support
   - Register router in `__main__.py`
   - Test: curl the endpoints

2. **Phase 2: Embedding Model Swap** (depends on Phase 1 for /embeddings)
   - Download bge-m3 model to `models/bge-m3/`
   - Update `niu_api/internal/embedding.py` to prefer bge-m3
   - Run `scripts/reindex_vectors.py` to migrate vectors.db
   - Test: verify new vectors are 1024-dim

3. **Phase 3: Reranker** (depends on Phase 2 being stable)
   - Implement `niu_api/internal/reranker.py`
   - Create `local_rerank_func` callable for LightRAG
   - Configure LightRAG with rerank function
   - Test: query with and without reranking, compare quality

4. **Phase 4: LightRAG Integration** (depends on all above)
   - Point LightRAG at `http://localhost:9876/llm/v1`
   - Set embedding_dim=1024
   - Pass rerank callable
   - End-to-end test: insert documents, query

### Key Risk: Embedding Dimension Mismatch

The biggest risk is the 384->1024 dimension change. If any component is not migrated, it will produce 384-dim vectors that cannot be compared with 1024-dim vectors. Mitigations:

1. **Dimension check on read:** Add assertion in `VectorSearchAdapter._search_once()`:
   ```python
   doc_vec = np.frombuffer(embedding_blob, dtype=np.float32)
   if len(doc_vec) != len(query_vec):
       logger.warning(f"Dimension mismatch: query={len(query_vec)}, doc={len(doc_vec)}, skipping {doc_id}")
       continue
   ```
2. **Re-indexing is idempotent:** Running the script twice is safe (just slow)
3. **Backup before migration:** `vectors.db.384dim.bak` preserves the old data
4. **Rollback:** If bge-m3 fails, switch back to MiniLM and restore from backup

### Files Changed Summary

| File | Action | Description |
|------|--------|-------------|
| `niu_api/llm_proxy.py` | CREATE | Generalized OpenAI-compatible proxy |
| `niu_api/internal/embedding.py` | MODIFY | Add bge-m3 model preference, `get_embedding_dim()` |
| `niu_api/internal/reranker.py` | CREATE | Local CrossEncoder reranker |
| `niu_api/__main__.py` | MODIFY | Register `llm_proxy_router` |
| `scripts/reindex_vectors.py` | CREATE | Vector DB re-indexing script |
| `models/bge-m3/` | DOWNLOAD | BAAI/bge-m3 model files (~570 MB) |
| `models/bge-reranker-v2-m3/` | DOWNLOAD | BAAI/bge-reranker-v2-m3 model files (~560 MB) |

---

## 9. Pluggable Model Configuration

Embedding 和 Reranker 模型通过配置文件切换，不改代码。主 Agent 读取系统管理手册后可帮用户切换。

### 9.1 配置位置

在 `~/.niu/preferences.json` 中新增 `lightrag` 配置段：

```json
{
  "lightrag": {
    "embedding": {
      "model": "bge-m3",
      "dimension": 1024,
      "max_tokens": 8192
    },
    "reranker": {
      "enabled": true,
      "model": "bge-reranker-v2-m3",
      "idle_timeout_seconds": 600
    }
  }
}
```

### 9.2 支持的模型列表

#### Embedding 模型

| model 值 | 维度 | 大小 | 多语言 | 说明 |
|----------|------|------|--------|------|
| `bge-m3` | 1024 | ~570MB | ✅ | 默认，LightRAG 推荐 |
| `bge-m3-large` | 1024 | ~1.3GB | ✅ | 更高质量，需要更多内存 |
| `minilm-l12` | 384 | ~480MB | ❌ | 旧模型，仅用于兼容 |

#### Reranker 模型

| model 值 | 大小 | 说明 |
|----------|------|------|
| `bge-reranker-v2-m3` | ~560MB | 默认，匹配 bge-m3 |
| `bge-reranker-v2-gemma` | ~1.2GB | 更高质量，需要更多内存 |
| `none` | 0 | 禁用 Reranker |

### 9.3 切换流程

1. 用户告诉主 Agent："我想换一个更好的 Embedding 模型"
2. 主 Agent 读取系统管理手册，了解可切换的模型列表
3. 主 Agent 修改 `~/.niu/preferences.json` 中的 `lightrag.embedding.model` 和 `dimension`
4. 主 Agent 提醒用户：切换 Embedding 模型后需要重新索引（运行 `scripts/reindex_vectors.py`）
5. 用户确认后，主 Agent 执行重索引
6. 重启应用生效

### 9.4 代码层实现

`niu_api/internal/embedding.py` 读取配置决定加载哪个模型：

```python
def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        config = load_preferences().get("lightrag", {}).get("embedding", {})
        model_name = config.get("model", "bge-m3")
        models_dir = get_models_dir()
        model_path = models_dir / model_name

        if model_path.exists():
            _model = SentenceTransformer(str(model_path))
        else:
            _model = SentenceTransformer(f"BAAI/{model_name}")
            _model.save(str(model_path))

        _model = _model.to(get_device())
    return _model
```

`niu_api/internal/reranker.py` 同理读取配置：

```python
def get_reranker():
    config = load_preferences().get("lightrag", {}).get("reranker", {})
    if not config.get("enabled", True):
        return None
    model_name = config.get("model", "bge-reranker-v2-m3")
    if model_name == "none":
        return None
    # ... load model
```

### 9.5 系统管理手册内容

以下内容写入系统管理手册（主 Agent 可读）：

```markdown
## LightRAG 模型配置

### Embedding 模型
- 配置位置：~/.niu/preferences.json → lightrag.embedding.model
- 默认：bge-m3（1024维，~570MB）
- 可选：bge-m3-large（更高质量，~1.3GB）
- 切换后必须运行：python scripts/reindex_vectors.py
- 切换后必须重启应用

### Reranker 模型
- 配置位置：~/.niu/preferences.json → lightrag.reranker.model
- 默认：bge-reranker-v2-m3（~560MB，懒加载）
- 可选：bge-reranker-v2-gemma（更高质量，~1.2GB）
- 禁用：设为 "none"
- 切换后重启应用即可，无需重索引

### 内存参考
- bge-m3 + bge-reranker-v2-m3：稳态 ~720MB，峰值 ~1.3GB
- bge-m3-large + bge-reranker-v2-gemma：稳态 ~1.4GB，峰值 ~2.6GB
- 如果用户电脑内存 < 8GB，建议使用默认模型
```
