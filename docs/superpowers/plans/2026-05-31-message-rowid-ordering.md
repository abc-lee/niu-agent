# Fix Message Ordering: Replace created_at with rowid

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix message ordering by sorting on SQLite rowid (write order) instead of created_at (unreliable timestamp).

**Architecture:** Modify `MessageStore.get_messages()` to use `ORDER BY rowid` for all queries, and change cursor-based pagination from `WHERE created_at < ?` to `WHERE rowid < ?`. Add `rowid` field to `Message` dataclass. No schema migration needed — rowid is SQLite built-in.

**Tech Stack:** Python, SQLite (aiosqlite), dataclasses

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `agent/session.py` | Modify | Core change: Message dataclass + MessageStore queries |
| `tests/test_p0/test_session.py` | Modify | Add ordering tests |
| `niu_api/compat.py` | No change | Uses UUID-based cursor, not affected |
| `niu_api/session.py` | No change | API layer passes through, not affected |
| `mcp-servers/session-manager/` | No change | MCP tools use UUID, not affected |

---

### Task 1: Add rowid field to Message dataclass

**Files:**
- Modify: `agent/session.py:19-35`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_p0/test_session.py`:

```python
def test_message_has_rowid_field():
    """Message dataclass must have a rowid field with default 0."""
    msg = Message(
        id="test-id",
        role="user",
        content="hello",
        created_at="2026-01-01T00:00:00",
    )
    assert hasattr(msg, "rowid")
    assert msg.rowid == 0

    msg_with_rowid = Message(
        id="test-id",
        role="user",
        content="hello",
        created_at="2026-01-01T00:00:00",
        rowid=42,
    )
    assert msg_with_rowid.rowid == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py::test_message_has_rowid_field -v`
Expected: FAIL with `TypeError: Message.__init__() got an unexpected keyword argument 'rowid'`

- [ ] **Step 3: Add rowid field to Message dataclass**

In `agent/session.py`, add `rowid: int = 0` after `created_at` field (line ~27):

```python
@dataclass
class Message:
    """A single message in the conversation."""
    id: str = ""
    role: str = ""
    content: str = ""
    tool_calls: list[dict] | None = None
    tool_results: list[dict] | None = None
    tool_call_id: str | None = None
    created_at: str = ""
    rowid: int = 0  # SQLite rowid, 0 = sentinel for "not loaded from DB" (real rowid starts at 1)
```

**Note**: `rowid=0` is a sentinel value meaning "not loaded from database". SQLite rowid starts at 1, so 0 will never appear in real DB data. This is safe for `to_dict()` / `asdict()` serialization — the extra `rowid` field won't break existing consumers (MessageResponse explicitly picks fields, dicts are lenient).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py::test_message_has_rowid_field -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/session.py tests/test_p0/test_session.py
git commit -m "feat: add rowid field to Message dataclass"
```

---

### Task 2: Fix get_messages() to use rowid ordering

**Files:**
- Modify: `agent/session.py:119-198`

This is the core fix. `get_messages()` currently uses `ORDER BY created_at DESC` and cursor-based pagination with `WHERE created_at < ?`. Change both to use `rowid`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_p0/test_session.py`:

```python
import asyncio
import aiosqlite
import os
import tempfile


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test_messages.db")


async def _init_store(db_path: str) -> MessageStore:
    """Create a MessageStore with real schema (including WAL mode and tool_call_id)."""
    store = MessageStore(db_path)
    await store.init_db()
    return store


async def _insert_message(db_path: str, msg_id: str, role: str, content: str, created_at: str):
    """Insert a message directly into the database (to control created_at independently of rowid)."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO messages (id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (msg_id, role, content, created_at),
        )
        await db.commit()


def test_messages_ordered_by_rowid_not_created_at(db_path):
    """
    Messages must be returned in rowid order (write order), not created_at order.

    Scenario: Insert messages with out-of-order created_at values.
    - Insert A at 2026-05-31T08:00 (rowid=1)
    - Insert B at 2026-05-30T22:00 (rowid=2, created_at EARLIER than A)
    - Insert C at 2026-05-31T08:01 (rowid=3)

    If sorted by created_at DESC: C, A, B (wrong — B comes after A despite being written second)
    If sorted by rowid DESC: C, B, A (correct — matches write order)
    """
    async def _run():
        store = await _init_store(db_path)

        # Write A first (earlier timestamp today)
        await _insert_message(db_path, "msg-a", "user", "Message A", "2026-05-31T08:00:00")
        # Write B second (LATER rowid, but EARLIER created_at — yesterday)
        await _insert_message(db_path, "msg-b", "user", "Message B", "2026-05-30T22:00:00")
        # Write C third (latest of both)
        await _insert_message(db_path, "msg-c", "user", "Message C", "2026-05-31T08:01:00")

        messages = await store.get_messages(limit=10)

        # Must return in write order (rowid DESC): C, B, A
        assert len(messages) == 3
        assert messages[0].id == "msg-c"
        assert messages[1].id == "msg-b"
        assert messages[2].id == "msg-a"

    asyncio.run(_run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py::test_messages_ordered_by_rowid_not_created_at -v`
Expected: FAIL — messages returned in created_at order (C, A, B) instead of rowid order (C, B, A)

- [ ] **Step 3: Modify get_messages() SQL queries**

In `agent/session.py`, modify `get_messages()` method (starting around line 119):

**Change 1**: Main query — replace `ORDER BY created_at DESC` with `ORDER BY rowid DESC`:

```python
# Line ~134: Change the main SELECT
query = """
    SELECT id, role, content, tool_calls, tool_results, tool_call_id, created_at, rowid
    FROM messages
    ORDER BY rowid DESC
    LIMIT ?
"""
```

**Change 2**: Cursor-based pagination — delete the old `created_at` cursor resolution code (lines 129-136: `SELECT created_at FROM messages WHERE id = ?` and `before_ts = before_row["created_at"]`) and replace the entire `get_messages()` body:

The complete new `get_messages()` method:

```python
async def get_messages(self, limit: Optional[int] = None, before_id: Optional[str] = None) -> List[Message]:
    """Get messages (chronological order by write sequence). If limit is None, return all messages.

    Pagination uses rowid (write order), not created_at timestamp.
    before_id is resolved to its rowid for cursor-based pagination.
    """
    _COLUMNS = "id, role, content, tool_calls, tool_results, tool_call_id, created_at, rowid"

    async with aiosqlite.connect(self.db_path) as db:
        db.row_factory = aiosqlite.Row

        if before_id:
            # Resolve before_id to its rowid for cursor-based pagination
            cursor = await db.execute(
                "SELECT rowid FROM messages WHERE id = ?",
                (before_id,),
            )
            before_row = await cursor.fetchone()
            if before_row:
                before_rowid = before_row[0]
                if limit is not None:
                    cursor = await db.execute(
                        f"""SELECT {_COLUMNS} FROM messages
                           WHERE rowid < ?
                           ORDER BY rowid DESC
                           LIMIT ?""",
                        (before_rowid, limit),
                    )
                else:
                    cursor = await db.execute(
                        f"""SELECT {_COLUMNS} FROM messages
                           WHERE rowid < ?
                           ORDER BY rowid DESC""",
                        (before_rowid,),
                    )
            else:
                # before_id not found, fall back to no cursor
                if limit is not None:
                    cursor = await db.execute(
                        f"""SELECT {_COLUMNS} FROM messages
                           ORDER BY rowid DESC
                           LIMIT ?""",
                        (limit,),
                    )
                else:
                    cursor = await db.execute(
                        f"""SELECT {_COLUMNS} FROM messages
                           ORDER BY rowid DESC"""
                    )
        else:
            if limit is not None:
                cursor = await db.execute(
                    f"""SELECT {_COLUMNS} FROM messages
                       ORDER BY rowid DESC
                       LIMIT ?""",
                    (limit,),
                )
            else:
                cursor = await db.execute(
                    f"""SELECT {_COLUMNS} FROM messages
                       ORDER BY rowid DESC"""
                )

        rows = await cursor.fetchall()

        messages = []
        for row in reversed(rows):  # Return in chronological order
            messages.append(
                Message(
                    id=row["id"],
                    role=row["role"],
                    content=row["content"] or "",
                    tool_calls=json.loads(row["tool_calls"] or "[]"),
                    tool_results=json.loads(row["tool_results"] or "[]"),
                    tool_call_id=row["tool_call_id"] if "tool_call_id" in row.keys() else "",
                    created_at=row["created_at"],
                    rowid=row["rowid"],
                )
            )

        return messages
```

**Important**: aiosqlite `row_factory=aiosqlite.Row` supports accessing columns by name with `row["rowid"]`. The `_COLUMNS` constant ensures `rowid` is always included in the SELECT.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py::test_messages_ordered_by_rowid_not_created_at -v`
Expected: PASS

- [ ] **Step 5: Run existing tests to verify no regression**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py -v`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add agent/session.py tests/test_p0/test_session.py
git commit -m "fix: message ordering uses rowid (write order) instead of created_at"
```

---

### Task 3: Test cursor-based pagination with rowid

**Files:**
- Modify: `tests/test_p0/test_session.py`

- [ ] **Step 1: Write the failing test**

```python
def test_pagination_cursor_uses_rowid(db_path):
    """
    Cursor-based pagination must use rowid, not created_at.

    Scenario:
    - Insert 5 messages with mixed created_at values
    - Request limit=2 (gets newest 2 by rowid)
    - Use last message's id as before_id
    - Request next page (should get the next 2 by rowid, not by created_at)
    """
    async def _run():
        store = await _init_store(db_path)

        # Insert 5 messages with out-of-order timestamps
        # rowid order: 1,2,3,4,5 — this is the true order
        await _insert_message(db_path, "m1", "user", "First", "2026-05-31T08:00:00")   # rowid=1
        await _insert_message(db_path, "m2", "user", "Second", "2026-05-30T22:00:00")  # rowid=2
        await _insert_message(db_path, "m3", "user", "Third", "2026-05-31T08:01:00")   # rowid=3
        await _insert_message(db_path, "m4", "user", "Fourth", "2026-05-30T23:00:00")  # rowid=4
        await _insert_message(db_path, "m5", "user", "Fifth", "2026-05-31T08:02:00")   # rowid=5

        # First page: limit=2, should get m5, m4 (newest by rowid)
        page1 = await store.get_messages(limit=2)
        assert len(page1) == 2
        assert page1[0].id == "m5"
        assert page1[1].id == "m4"

        # Second page: before_id=m4's id, should get m3, m2
        page2 = await store.get_messages(limit=2, before_id="m4")
        assert len(page2) == 2
        assert page2[0].id == "m3"
        assert page2[1].id == "m2"

        # Third page: before_id=m2's id, should get m1
        page3 = await store.get_messages(limit=2, before_id="m2")
        assert len(page3) == 1
        assert page3[0].id == "m1"

    asyncio.run(_run())
```

- [ ] **Step 2: Run test to verify it passes**

This test should pass if Task 2 was implemented correctly. If it fails, the cursor pagination is still using created_at.

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py::test_pagination_cursor_uses_rowid -v`
Expected: PASS (if Task 2 is correct) or FAIL (if cursor still uses created_at)

- [ ] **Step 3: If test fails, fix cursor logic**

If the cursor pagination is still wrong, the issue is in the `before_id` resolution. Verify the subquery `SELECT rowid FROM messages WHERE id = ?` returns the correct rowid.

- [ ] **Step 4: Commit (if changes were needed)**

```bash
git add agent/session.py tests/test_p0/test_session.py
git commit -m "fix: cursor-based pagination uses rowid"
```

---

### Task 4: Test deleted messages with rowid gaps

**Files:**
- Modify: `tests/test_p0/test_session.py`

- [ ] **Step 1: Write the test**

```python
def test_rowid_ordering_with_deleted_messages(db_path):
    """
    Deleting messages creates rowid gaps but must not break ordering.

    Scenario:
    - Insert 5 messages (rowid 1-5)
    - Delete messages 2 and 4
    - Remaining: rowid 1, 3, 5
    - get_messages() must return them in rowid DESC order: 5, 3, 1
    """
    async def _run():
        store = await _init_store(db_path)

        for i in range(1, 6):
            await _insert_message(
                db_path, f"m{i}", "user", f"Message {i}",
                f"2026-05-31T08:00:{i:02d}",
            )

        # Delete m2 and m4
        await store.delete_messages_by_ids(["m2", "m4"])

        messages = await store.get_messages(limit=10)
        assert len(messages) == 3
        assert messages[0].id == "m5"
        assert messages[1].id == "m3"
        assert messages[2].id == "m1"

    asyncio.run(_run())
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py::test_rowid_ordering_with_deleted_messages -v`
Expected: PASS (SQLite rowid gaps don't affect ORDER BY)

- [ ] **Step 3: Commit**

```bash
git add tests/test_p0/test_session.py
git commit -m "test: verify rowid ordering with deleted messages"
```

---

### Task 5: Test same-second messages maintain write order

**Files:**
- Modify: `tests/test_p0/test_session.py`

- [ ] **Step 1: Write the test**

```python
def test_same_second_messages_maintain_write_order(db_path):
    """
    Messages with identical created_at must still be ordered by write order (rowid).

    This is the original bug: same-second messages had indeterminate ordering.
    """
    async def _run():
        store = await _init_store(db_path)

        # Insert 3 messages with IDENTICAL created_at
        ts = "2026-05-31T08:00:00"
        await _insert_message(db_path, "first", "user", "Written first", ts)
        await _insert_message(db_path, "second", "assistant", "Written second", ts)
        await _insert_message(db_path, "third", "user", "Written third", ts)

        messages = await store.get_messages(limit=10)
        assert len(messages) == 3
        # Must be in write order (rowid DESC): third, second, first
        assert messages[0].id == "third"
        assert messages[1].id == "second"
        assert messages[2].id == "first"

    asyncio.run(_run())
```

- [ ] **Step 2: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py::test_same_second_messages_maintain_write_order -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_p0/test_session.py
git commit -m "test: verify same-second messages maintain write order"
```

---

### Task 6: Fix all remaining created_at ORDER BY in session.py

**Files:**
- Modify: `agent/session.py`

After Tasks 1-5, the main `get_messages()` is fixed. This task audits ALL remaining `created_at` sort references in the file.

- [ ] **Step 1: Search for remaining created_at ORDER BY**

Run: `grep -n "created_at" REDACTED_USER_PATH/tools/ai-bot/agent/session.py`

Expected results and actions:
- Any `ORDER BY created_at` → change to `ORDER BY rowid`
- Any `WHERE created_at` used for filtering/pagination → change to `WHERE rowid`
- `INSERT INTO messages ... created_at` → keep as-is (we still write the timestamp)
- `created_at` in Message dataclass field → keep as-is
- `created_at` in SELECT column list → keep as-is (still need the value)

- [ ] **Step 2: Handle idx_messages_created_at index**

The `idx_messages_created_at` index (line 76-78) is no longer used for sorting queries. Keep it for backward compatibility (existing DBs have it), but add a comment marking it as deprecated:

```python
# DEPRECATED: no longer used for ORDER BY (switched to rowid), kept for existing DBs
await db.execute("""
    CREATE INDEX IF NOT EXISTS idx_messages_created_at
    ON messages(created_at ASC)
""")
```

- [ ] **Step 3: Fix each remaining ORDER BY occurrence**

For each remaining `ORDER BY created_at` or `WHERE created_at` found:
1. Read the surrounding function to understand context
2. Change `ORDER BY created_at` to `ORDER BY rowid`
3. Change pagination cursor from `created_at` to `rowid`
4. Add `rowid` to the SELECT column list
5. Update the Message construction to include `rowid=row["rowid"]`

- [ ] **Step 3: Run all session tests**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_p0/test_session.py -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add agent/session.py
git commit -m "fix: replace all remaining created_at ORDER BY with rowid in session.py"
```

---

### Task 7: Verify other files don't depend on created_at ordering

**Files:**
- Verify: `niu_api/compat.py`, `niu_api/session.py`, `mcp-servers/session-manager/src/niu_session_manager/__init__.py`

- [ ] **Step 1: Search for created_at ORDER BY in other files**

Run:
```bash
grep -rn "ORDER BY.*created_at\|created_at.*DESC\|created_at.*ASC" REDACTED_USER_PATH/tools/ai-bot/niu_api/ REDACTED_USER_PATH/tools/ai-bot/mcp-servers/session-manager/
```

- [ ] **Step 2: Fix any occurrences found**

If any other file has `ORDER BY created_at`, it must also be changed to `ORDER BY rowid`. Add `rowid` to those SELECT clauses as well.

- [ ] **Step 3: Run all tests**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/ -v -k "session"`
Expected: All session-related tests pass

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "fix: replace created_at ORDER BY with rowid in all files"
```

---

### Task 8: Integration test with real database

**Files:**
- No code changes, manual verification

- [ ] **Step 1: Start the application**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && go run main.go`

- [ ] **Step 2: Verify message ordering in the UI**

1. Open the Electron UI
2. Check that messages display in the correct write order
3. Scroll up to load earlier messages — verify pagination works
4. Send a new message — verify it appears at the bottom (latest)

- [ ] **Step 3: Verify auto-tidy pipeline still works**

Wait for auto-tidy to run (or trigger it), then verify:
1. Entity-extractor processes messages in correct order
2. Dream-evolver cursor advances correctly
3. Journal-agent processes messages in correct order
4. Context-manager compression doesn't break ordering

- [ ] **Step 4: Kill all processes after testing**

Run: `pkill -f "niu_api" ; pkill -f "niu.exe" ; pkill -f "python.*niu"`
