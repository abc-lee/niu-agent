# Chat Session Concurrency Lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent concurrent `runner.chat()` calls from corrupting shared state (handler, tool_lifecycle, SQLite, face model) by serializing all chat requests at the API layer.

**Architecture:** Add an `asyncio.Lock` in the API layer (`niu_api/compat.py` and `niu_api/chat.py`) that guards `runner.chat()` calls. When a second request arrives while the first is still processing, it waits for the lock instead of running in parallel. This is NOT a queue in photo-server — it's a serialization gate at the entry point to the agent runner, before any tool calls happen.

**Tech Stack:** Python asyncio, FastAPI

---

## File Structure

| File | Responsibility |
|------|---------------|
| `niu_api/compat.py` | Add lock around `chat_session()` endpoint |
| `niu_api/chat.py` | Add lock around `chat()` and `chat_sync()` endpoints |
| `tests/test_chat_lock.py` | Test concurrent request serialization |

---

### Task 1: Add asyncio.Lock to compat.py chat_session

**Files:**
- Modify: `niu_api/compat.py`

The `chat_session()` endpoint is the primary entry point used by both the chat window and the spirit widget. It calls `runner.chat()` via `asyncio.to_thread()`.

- [ ] **Step 1: Add module-level lock and guard chat_session**

In `niu_api/compat.py`, add a module-level `asyncio.Lock` and wrap the `runner.chat()` call:

```python
# At module level (after imports, around line 30)
import asyncio
_chat_lock = asyncio.Lock()
```

Then in `chat_session()`, wrap the `sync_chat` call:

```python
    try:
        async with _chat_lock:
            full_reply = await asyncio.to_thread(sync_chat)
    except Exception as e:
        import traceback
        logger.error(f"Chat error: {e}\n{traceback.format_exc()}")
        full_reply = f"Error: {str(e)}"
```

The key change: `await asyncio.to_thread(sync_chat)` is now inside `async with _chat_lock:`. This means:
- First request acquires the lock and runs
- Second request waits at `async with _chat_lock:` until the first completes
- No shared state corruption, no SQLite write conflicts

- [ ] **Step 2: Verify the change doesn't break existing behavior**

Run the app and test a single chat message. The lock is uncontended in the single-request case, so behavior is identical.

```bash
cd E:/tools/ai-bot && python -c "from niu_api.compat import _chat_lock; print(type(_chat_lock))"
```

Expected: `<class 'asyncio.locks.Lock'>`

- [ ] **Step 3: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: add asyncio.Lock to chat_session endpoint for request serialization"
```

---

### Task 2: Add asyncio.Lock to chat.py endpoints

**Files:**
- Modify: `niu_api/chat.py`

The `chat()` (streaming) and `chat_sync()` endpoints also call `runner.chat()`. They need the same lock.

- [ ] **Step 1: Add module-level lock and guard both endpoints**

In `niu_api/chat.py`, add a module-level `asyncio.Lock`:

```python
# At module level (after imports)
import asyncio
_chat_lock = asyncio.Lock()
```

For `chat()` (streaming endpoint), the lock must be acquired before starting the generator, and held for the entire streaming response:

```python
async def chat(request: ChatRequest) -> StreamingResponse:
    # ... validation code unchanged ...

    runner = get_or_create_runner()
    session_id = request.session_id or "default"

    async def generate():
        await _chat_lock.acquire()
        try:
            reply_chunks = []

            def sync_chat():
                return runner.chat(session_id, request.message, stream=True)

            loop = asyncio.get_event_loop()
            gen = await loop.run_in_executor(None, sync_chat)

            for chunk in gen:
                if chunk:
                    reply_chunks.append(chunk)
                    yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            yield f"data: {json.dumps({'done': True, 'session_id': session_id})}\n\n"
        finally:
            _chat_lock.release()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

For `chat_sync()`, same pattern as `chat_session()`:

```python
    try:
        async with _chat_lock:
            full_reply = await asyncio.to_thread(sync_chat)
    except Exception as e:
        # ... error handling unchanged ...
```

- [ ] **Step 2: Verify**

```bash
cd E:/tools/ai-bot && python -c "from niu_api.chat import _chat_lock; print(type(_chat_lock))"
```

Expected: `<class 'asyncio.locks.Lock'>`

- [ ] **Step 3: Commit**

```bash
git add niu_api/chat.py
git commit -m "feat: add asyncio.Lock to chat/chat_sync endpoints for request serialization"
```

---

### Task 3: Write concurrency test

**Files:**
- Create: `tests/test_chat_lock.py`

- [ ] **Step 1: Write test that verifies requests are serialized**

```python
#!/usr/bin/env python3
"""Test that chat requests are serialized via asyncio.Lock"""

import asyncio
import time


async def test_chat_lock_serializes():
    """Two concurrent requests should be serialized, not parallel"""
    from niu_api.compat import _chat_lock

    results = []
    
    async def simulated_request(name: str, duration: float):
        async with _chat_lock:
            results.append(f"{name}-start")
            await asyncio.sleep(duration)
            results.append(f"{name}-end")

    # Launch two requests concurrently
    await asyncio.gather(
        simulated_request("A", 0.1),
        simulated_request("B", 0.1),
    )

    # If serialized: A-start, A-end, B-start, B-end (or B first, then A)
    # If parallel: A-start, B-start, A-end, B-end (interleaved)
    # Check that no two "start" events appear without an "end" between them
    starts = [i for i, r in enumerate(results) if r.endswith("-start")]
    ends = [i for i, r in enumerate(results) if r.endswith("-end")]
    
    # First start must come before first end
    assert starts[0] < ends[0], f"Not serialized: {results}"
    # Second start must come after first end
    assert starts[1] > ends[0], f"Not serialized: {results}"
    print(f"PASS: Requests serialized: {results}")


async def test_chat_lock_uncontended():
    """Single request should work normally"""
    from niu_api.compat import _chat_lock

    async with _chat_lock:
        pass  # Should complete immediately
    
    assert not _chat_lock.locked()
    print("PASS: Uncontended lock works")


if __name__ == "__main__":
    asyncio.run(test_chat_lock_serializes())
    asyncio.run(test_chat_lock_uncontended())
```

- [ ] **Step 2: Run test**

```bash
cd E:/tools/ai-bot && python tests/test_chat_lock.py
```

Expected: Both tests PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_chat_lock.py
git commit -m "test: add concurrency serialization test for chat lock"
```

---

### Task 4: Use a shared lock across both API modules

**Files:**
- Modify: `niu_api/compat.py`
- Modify: `niu_api/chat.py`

Currently Task 1 and Task 2 create **separate** locks in each module. But both modules call the same `runner.chat()` on the same singleton runner. They need to share one lock.

- [ ] **Step 1: Create a shared lock in a common location**

In `niu_api/compat.py`, the lock is already defined as `_chat_lock`. Export it and import it in `chat.py`.

In `niu_api/compat.py`, ensure the lock is accessible:

```python
# Already exists from Task 1
_chat_lock = asyncio.Lock()
```

In `niu_api/chat.py`, replace the module-level lock with an import:

```python
# Replace: _chat_lock = asyncio.Lock()
# With:
from niu_api.compat import _chat_lock
```

- [ ] **Step 2: Verify both modules share the same lock object**

```bash
cd E:/tools/ai-bot && python -c "
from niu_api.compat import _chat_lock as lock1
from niu_api.chat import _chat_lock as lock2
assert lock1 is lock2, 'Locks are different objects!'
print('PASS: Same lock object shared across modules')
"
```

Expected: `PASS: Same lock object shared across modules`

- [ ] **Step 3: Commit**

```bash
git add niu_api/compat.py niu_api/chat.py
git commit -m "fix: share single asyncio.Lock across compat.py and chat.py"
```

---

## Self-Review

**1. Spec coverage:** The problem is concurrent `runner.chat()` calls corrupting shared state. The solution is a single `asyncio.Lock` shared across all API endpoints that call `runner.chat()`. Tasks 1-2 add the lock, Task 3 tests it, Task 4 ensures it's a single shared lock. All covered.

**2. Placeholder scan:** No TBD, TODO, or placeholder patterns found. All code is complete.

**3. Type consistency:** `_chat_lock` is `asyncio.Lock` in all references. `chat_session()` returns `ChatResponse`. `chat()` returns `StreamingResponse`. No conflicts.

## Design Notes

**Why asyncio.Lock and not threading.Lock?**
The API endpoints are `async def` running on the asyncio event loop. `asyncio.Lock` is the correct primitive — it blocks the coroutine without blocking the event loop thread. A `threading.Lock` would block the event loop thread itself, preventing any other async work from proceeding.

**Why not a queue?**
A queue would add complexity (ordering, priority, cancellation) for no benefit. We just need serialization — one request at a time. A lock is the simplest correct solution.

**Why at the API layer and not in photo-server?**
The concurrency problem is not specific to photo-server. It affects the entire `NiuRunner` + `NiuHandler` shared state. The lock must be at the entry point to `runner.chat()`, which is the API layer. Photo-server's SQLite and face model are downstream resources that benefit from the upstream serialization.

**What about timeout?**
If a request takes too long (e.g., LLM hangs), the lock holder blocks all subsequent requests. This is acceptable because:
1. The existing behavior without the lock is worse (data corruption)
2. The LLM already has a timeout mechanism in `agent_loop.py`
3. If needed, a timeout on the lock acquisition can be added later: `await asyncio.wait_for(_chat_lock.acquire(), timeout=300)`
