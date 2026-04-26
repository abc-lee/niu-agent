# Notes JSON Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SQLite notes storage with JSON file in workspace, sync to LightRAG via SkillSync, Agent operates via bash + Skill.

**Architecture:** Notes stored as JSON array in `{workspace}/notes/notes.json`. Frontend API reads/writes JSON file instead of SQLite. SkillSync scans notes file and injects as `knowledge` entities into LightRAG. Agent uses bash to read/write notes.json guided by a Skill file. All SQLite code removed.

**Tech Stack:** Python (FastAPI, LightRAGIngester), JSON, SkillSync

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `niu_api/notes.py` | JSON read/write layer | **Rewrite** (129→~80 lines) |
| `niu_api/notes_api.py` | REST API endpoints + LightRAG sync | **Rewrite** (114→~90 lines) |
| `niu_api/__main__.py` | Remove SQLite init, keep router | **Modify** 3 lines |
| `agent/injector/sync.py` | Add notes scanning to SkillSync | **Modify** ~30 lines |
| `niu_api/internal/lightrag_pipeline.py` | Remove `note` source_type branch | **Modify** 3 lines |
| `memory/skills/note-management.md` | Agent guidance for bash notes ops | **Create** |
| `tests/test_notes_json.py` | Tests for JSON storage + API + sync | **Create** |
| `tests/test_phase02_lightrag_migration.py` | Update notes tests | **Modify** |
| `tests/test_lightrag_pipeline.py` | Remove `[Note:]` prefix test | **Modify** |

---

### Task 1: Rewrite notes.py — JSON storage layer

**Files:**
- Rewrite: `niu_api/notes.py`
- Create: `tests/test_notes_json.py` (storage tests only)

- [ ] **Step 1: Write failing tests for JSON storage**

```python
"""Tests for notes JSON storage layer."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


class TestNotesJsonStorage:
    """Test niu_api.notes JSON read/write operations."""

    @pytest.fixture
    def tmp_workspace(self, tmp_path):
        """Create a temporary workspace with notes directory."""
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        return tmp_path

    def test_read_notes_returns_empty_when_file_missing(self, tmp_workspace):
        """Reading notes when file doesn't exist returns empty list."""
        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import read_notes
            result = read_notes()
            assert result == []

    def test_read_notes_returns_existing_notes(self, tmp_workspace):
        """Reading notes returns all notes from JSON file."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([
            {"id": "n1", "content": "Buy milk", "tags": ["shopping"],
             "created_at": "2026-04-26T10:00:00", "updated_at": None}
        ]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import read_notes
            result = read_notes()
            assert len(result) == 1
            assert result[0]["id"] == "n1"
            assert result[0]["content"] == "Buy milk"

    def test_create_note_appends_to_file(self, tmp_workspace):
        """Creating a note appends it to the JSON file."""
        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import create_note
            result = create_note("n1", "Buy milk", tags=["shopping"])
            assert result["id"] == "n1"
            assert result["status"] == "created"

            # Verify file was written
            notes_file = tmp_workspace / "notes" / "notes.json"
            notes = json.loads(notes_file.read_text(encoding="utf-8"))
            assert len(notes) == 1
            assert notes[0]["content"] == "Buy milk"

    def test_create_note_creates_directory_if_missing(self, tmp_workspace):
        """Creating a note creates notes/ directory if it doesn't exist."""
        # Remove notes dir
        notes_dir = tmp_workspace / "notes"
        if notes_dir.exists():
            notes_dir.rmdir()

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import create_note
            create_note("n1", "Test")
            assert notes_dir.exists()

    def test_update_note_modifies_content(self, tmp_workspace):
        """Updating a note changes its content and updated_at."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([
            {"id": "n1", "content": "Buy milk", "tags": [],
             "created_at": "2026-04-26T10:00:00", "updated_at": None}
        ]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import update_note
            result = update_note("n1", content="Buy milk and eggs")
            assert result["status"] == "updated"

            notes = json.loads(notes_file.read_text(encoding="utf-8"))
            assert notes[0]["content"] == "Buy milk and eggs"
            assert notes[0]["updated_at"] is not None

    def test_update_note_not_found(self, tmp_workspace):
        """Updating a nonexistent note returns not_found."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import update_note
            result = update_note("missing", content="test")
            assert result["status"] == "not_found"

    def test_delete_note_removes_from_file(self, tmp_workspace):
        """Deleting a note removes it from the JSON file."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([
            {"id": "n1", "content": "Buy milk", "tags": [],
             "created_at": "2026-04-26T10:00:00", "updated_at": None},
            {"id": "n2", "content": "Call mom", "tags": [],
             "created_at": "2026-04-26T11:00:00", "updated_at": None}
        ]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import delete_note
            result = delete_note("n1")
            assert result["status"] == "deleted"

            notes = json.loads(notes_file.read_text(encoding="utf-8"))
            assert len(notes) == 1
            assert notes[0]["id"] == "n2"

    def test_delete_note_not_found(self, tmp_workspace):
        """Deleting a nonexistent note returns not_found."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import delete_note
            result = delete_note("missing")
            assert result["status"] == "not_found"

    def test_list_notes_returns_all_ordered(self, tmp_workspace):
        """list_notes returns all notes ordered by created_at DESC."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([
            {"id": "n1", "content": "First", "tags": [],
             "created_at": "2026-04-26T10:00:00", "updated_at": None},
            {"id": "n2", "content": "Second", "tags": [],
             "created_at": "2026-04-26T12:00:00", "updated_at": None}
        ]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import list_notes
            result = list_notes()
            assert len(result) == 2
            # Most recent first
            assert result[0]["id"] == "n2"

    def test_get_note_returns_single(self, tmp_workspace):
        """get_note returns a single note by ID."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([
            {"id": "n1", "content": "Buy milk", "tags": [],
             "created_at": "2026-04-26T10:00:00", "updated_at": None}
        ]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import get_note
            result = get_note("n1")
            assert result["id"] == "n1"

    def test_get_note_returns_none_for_missing(self, tmp_workspace):
        """get_note returns None for nonexistent ID."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([]), encoding="utf-8")

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            from niu_api.notes import get_note
            result = get_note("missing")
            assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_notes_json.py::TestNotesJsonStorage -v`
Expected: FAIL — `niu_api.notes` still uses SQLite, functions like `read_notes` don't exist

- [ ] **Step 3: Rewrite notes.py as JSON storage layer**

```python
"""
Notes Store - JSON-based sticky notes storage

便签数据持久化，存储在 {workspace}/notes/notes.json
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

from loguru import logger


def _get_notes_path() -> Path:
    """返回 {workspace}/notes/notes.json 路径"""
    ws = os.environ.get("WORKSPACE_PATH", "")
    if ws:
        return Path(ws) / "notes" / "notes.json"
    # Fallback: ~/.niu/notes/notes.json
    home = os.path.expanduser("~")
    return Path(home) / ".niu" / "notes" / "notes.json"


def _ensure_dir():
    """确保 notes 目录存在"""
    notes_path = _get_notes_path()
    notes_path.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write(data: list):
    """原子写入：先写临时文件再 rename"""
    notes_path = _get_notes_path()
    _ensure_dir()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json",
        dir=str(notes_path.parent),
        delete=False,
        encoding="utf-8",
    ) as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        temp_path = f.name
    # Windows: rename fails if target exists, so delete first
    if notes_path.exists():
        notes_path.unlink()
    Path(temp_path).rename(notes_path)


def read_notes() -> list[dict]:
    """读取所有便签，文件不存在返回空列表"""
    notes_path = _get_notes_path()
    if not notes_path.exists():
        return []
    try:
        return json.loads(notes_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.error(f"[Notes] Failed to read {notes_path}: {e}")
        return []


def write_notes(notes: list[dict]) -> None:
    """原子写入所有便签"""
    _atomic_write(notes)


def create_note(note_id: str, content: str, tags: list[str] = None, created_at: str = None) -> Dict:
    """追加一条便签"""
    if created_at is None:
        created_at = datetime.now().isoformat()
    if tags is None:
        tags = []

    notes = read_notes()
    new_note = {
        "id": note_id,
        "content": content,
        "tags": tags,
        "created_at": created_at,
        "updated_at": None,
    }
    notes.append(new_note)
    _atomic_write(notes)
    logger.info(f"[Notes] Created note: {note_id}")
    return {"id": note_id, "status": "created"}


def update_note(note_id: str, content: str = None, tags: list[str] = None) -> Dict:
    """更新便签内容或标签"""
    notes = read_notes()
    for note in notes:
        if note["id"] == note_id:
            if content is not None:
                note["content"] = content
            if tags is not None:
                note["tags"] = tags
            note["updated_at"] = datetime.now().isoformat()
            _atomic_write(notes)
            logger.info(f"[Notes] Updated note: {note_id}")
            return {"id": note_id, "status": "updated"}

    return {"id": note_id, "status": "not_found"}


def delete_note(note_id: str) -> Dict:
    """删除便签，同时删除 LightRAG 实体"""
    notes = read_notes()
    new_notes = [n for n in notes if n["id"] != note_id]
    if len(new_notes) == len(notes):
        return {"id": note_id, "status": "not_found"}

    _atomic_write(new_notes)
    logger.info(f"[Notes] Deleted note: {note_id}")

    # 同步删除 LightRAG 实体
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        adapter = LightRAGAdapter()
        adapter.delete_entity(f"note:{note_id}")
        logger.info(f"[Notes] Deleted note entity from LightRAG: note:{note_id}")
    except Exception as e:
        logger.warning(f"[Notes] LightRAG delete failed for note:{note_id}: {e}")

    return {"id": note_id, "status": "deleted"}


def list_notes() -> List[Dict]:
    """列出所有便签（按 created_at DESC 排序）"""
    notes = read_notes()
    return sorted(notes, key=lambda n: n.get("created_at", ""), reverse=True)


def get_note(note_id: str) -> Optional[Dict]:
    """获取单条便签"""
    notes = read_notes()
    for note in notes:
        if note["id"] == note_id:
            return note
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_notes_json.py::TestNotesJsonStorage -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add niu_api/notes.py tests/test_notes_json.py
git commit -m "feat: rewrite notes.py from SQLite to JSON storage"
```

---

### Task 2: Rewrite notes_api.py — REST endpoints + LightRAG sync

**Files:**
- Rewrite: `niu_api/notes_api.py`
- Modify: `tests/test_notes_json.py` (add API tests)

- [ ] **Step 1: Write failing tests for API endpoints**

Append to `tests/test_notes_json.py`:

```python
# ============== API endpoint tests ==============


class TestNotesApiEndpoints:
    """Test notes_api.py REST endpoints with JSON storage."""

    @pytest.fixture
    def tmp_workspace(self, tmp_path):
        """Create a temporary workspace with notes directory."""
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        return tmp_path

    @pytest.mark.asyncio
    async def test_create_note_endpoint(self, tmp_workspace):
        """POST /api/notes creates a note in JSON file."""
        from fastapi.testclient import TestClient
        from niu_api.__main__ import app

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            client = TestClient(app)
            response = client.post("/api/notes", json={
                "id": "n1",
                "content": "Buy milk",
                "tags": ["shopping"],
                "createdAt": 1745649600000,
            })
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"

            # Verify JSON file
            notes_file = tmp_workspace / "notes" / "notes.json"
            notes = json.loads(notes_file.read_text(encoding="utf-8"))
            assert len(notes) == 1
            assert notes[0]["content"] == "Buy milk"

    @pytest.mark.asyncio
    async def test_list_notes_endpoint(self, tmp_workspace):
        """GET /api/notes returns all notes."""
        # Pre-populate
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([
            {"id": "n1", "content": "First", "tags": [],
             "created_at": "2026-04-26T10:00:00", "updated_at": None}
        ]), encoding="utf-8")

        from fastapi.testclient import TestClient
        from niu_api.__main__ import app

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            client = TestClient(app)
            response = client.get("/api/notes")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "ok"
            assert len(data["notes"]) == 1

    @pytest.mark.asyncio
    async def test_delete_note_endpoint(self, tmp_workspace):
        """DELETE /api/notes/{id} removes note from JSON file."""
        notes_file = tmp_workspace / "notes" / "notes.json"
        notes_file.write_text(json.dumps([
            {"id": "n1", "content": "Buy milk", "tags": [],
             "created_at": "2026-04-26T10:00:00", "updated_at": None}
        ]), encoding="utf-8")

        from fastapi.testclient import TestClient
        from niu_api.__main__ import app

        with patch.dict("os.environ", {"WORKSPACE_PATH": str(tmp_workspace)}):
            with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter:
                mock_adapter.return_value.delete_entity.return_value = {"status": "ok"}
                client = TestClient(app)
                response = client.delete("/api/notes/n1")
                assert response.status_code == 200

                notes = json.loads(notes_file.read_text(encoding="utf-8"))
                assert len(notes) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_notes_json.py::TestNotesApiEndpoints -v`
Expected: FAIL — `notes_api.py` still imports from old SQLite `notes.py`, `sync_note_to_kg` uses `ainsert` instead of `LightRAGIngester`

- [ ] **Step 3: Rewrite notes_api.py**

```python
"""
Notes API - Sticky notes CRUD endpoints with LightRAG sync
"""

import asyncio

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from loguru import logger

from niu_api.notes import create_note, update_note, delete_note, list_notes, get_note

router = APIRouter(prefix="/api", tags=["notes"])


class NoteCreateRequest(BaseModel):
    id: str
    content: str
    tags: list[str] = []
    createdAt: float  # 前端传 ms 时间戳


class NoteUpdateRequest(BaseModel):
    id: str
    content: str
    tags: list[str] = []
    updatedAt: float  # 前端传 ms 时间戳


@router.post("/notes")
async def api_create_note(request: NoteCreateRequest, background_tasks: BackgroundTasks):
    """Create a new sticky note"""
    try:
        from datetime import datetime
        created_at = datetime.fromtimestamp(request.createdAt / 1000).isoformat()

        result = create_note(
            note_id=request.id,
            content=request.content,
            tags=request.tags,
            created_at=created_at,
        )

        # LightRAG 同步（后台任务）
        background_tasks.add_task(
            asyncio.to_thread, sync_note_to_lightrag,
            request.id, request.content, request.tags,
        )

        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Create failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notes")
async def api_list_notes():
    """List all sticky notes"""
    try:
        notes = list_notes()
        return {"status": "ok", "notes": notes}
    except Exception as e:
        logger.error(f"[Notes] List failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/notes/{note_id}")
async def api_get_note(note_id: str):
    """Get a single note"""
    note = get_note(note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return {"status": "ok", "note": note}


@router.put("/notes/{note_id}")
async def api_update_note(note_id: str, request: NoteUpdateRequest, background_tasks: BackgroundTasks):
    """Update a sticky note"""
    try:
        result = update_note(
            note_id=note_id,
            content=request.content,
            tags=request.tags,
        )

        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Note not found")

        # LightRAG 同步（后台任务）
        background_tasks.add_task(
            asyncio.to_thread, sync_note_to_lightrag,
            note_id, request.content, request.tags,
        )

        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Update failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/notes/{note_id}")
async def api_delete_note(note_id: str):
    """Delete a sticky note"""
    try:
        result = delete_note(note_id=note_id)
        if result["status"] == "not_found":
            raise HTTPException(status_code=404, detail="Note not found")
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.error(f"[Notes] Delete failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def sync_note_to_lightrag(note_id: str, content: str, tags: list[str] = None):
    """便签写入 LightRAG 知识图谱。

    使用 LightRAGIngester.inject_entity() 进行结构化注入，
    entity_type="knowledge"，命名格式 note:{id}。
    """
    try:
        from niu_api.internal.lightrag_adapter import LightRAGIngester

        ingester = LightRAGIngester()

        # 构建描述：内容 + 标签
        description = content
        if tags:
            description += f" | 标签: {', '.join(tags)}"

        result = ingester.inject_entity(
            name=f"note:{note_id}",
            entity_type="knowledge",
            description=description,
            source_id=f"note:{note_id}",
            chunk_content=description,
            file_path=f"note://{note_id}",
        )
        if result.get("status") == "ok":
            logger.info(f"[Notes] LightRAG sync: note:{note_id}")
        else:
            logger.warning(f"[Notes] LightRAG sync failed for {note_id}: {result.get('message', '')}")
    except Exception as e:
        logger.warning(f"[Notes] LightRAG sync failed for {note_id}: {e}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_notes_json.py -v`
Expected: PASS (all storage + API tests)

- [ ] **Step 5: Commit**

```bash
git add niu_api/notes_api.py tests/test_notes_json.py
git commit -m "feat: rewrite notes_api.py with JSON storage and LightRAGIngester sync"
```

---

### Task 3: Remove SQLite init from __main__.py

**Files:**
- Modify: `niu_api/__main__.py` (lines 35, 59-62, 235)

- [ ] **Step 1: Remove notes DB initialization**

In `niu_api/__main__.py`, remove these 3 sections:

1. **Line 35**: Remove `from niu_api.notes_api import router as notes_router` — but keep it, we still need the router. Only remove the DB init.

2. **Lines 59-62**: Remove the notes DB init block:
```python
    # 1.5. Initialize notes database
    from niu_api.notes import init_db as notes_init_db
    await notes_init_db()
    logger.info("Notes DB initialized")
```

Replace with a comment:
```python
    # 1.5. Notes use JSON storage (no DB init needed)
```

3. **Line 235**: Keep `app.include_router(notes_router)  # Notes API` unchanged.

- [ ] **Step 2: Verify no other references to init_db or aiosqlite in notes**

Run: `cd E:/tools/ai-bot && grep -rn "init_db\|aiosqlite" niu_api/notes.py niu_api/notes_api.py`
Expected: No matches (init_db and aiosqlite removed)

- [ ] **Step 3: Run existing tests to verify nothing broke**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_notes_json.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add niu_api/__main__.py
git commit -m "feat: remove SQLite notes DB init from startup"
```

---

### Task 4: Remove `[Note:]` prefix from lightrag_pipeline.py

**Files:**
- Modify: `niu_api/internal/lightrag_pipeline.py` (lines 94-96)
- Modify: `tests/test_lightrag_pipeline.py` (lines 400-419)

- [ ] **Step 1: Remove the `note` source_type branch**

In `niu_api/internal/lightrag_pipeline.py`, the `_preprocess_content()` function at line 94-96 has:

```python
    elif task.source_type == "note":
        note_id = task.source_id.split(":", 1)[1] if ":" in task.source_id else task.source_id
        return f"[Note: {note_id}]\n{task.content}"
```

Remove these 3 lines. The function should now only handle `photo`, `document`, and `else` (file/other).

Also update the docstring at line 13: remove "photo/note/document" → "photo/document".

- [ ] **Step 2: Update the IngestTask docstring**

At line 39-40, change:
```python
        source_id: Application-level ID (e.g., "photo:123", "note:shopping").
        source_type: Source category ("photo", "note", "document", "file").
```

To:
```python
        source_id: Application-level ID (e.g., "photo:123", "document:report").
        source_type: Source category ("photo", "document", "file").
```

- [ ] **Step 3: Update test_lightrag_pipeline.py**

Remove the `test_ingest_note_adds_prefix` test (lines 400-419) and any `source_type="note"` references in other tests. Replace `note` source_type with `file` in tests that use it as a generic source type.

In the `test_source_type_enum` test (around line 52), change:
```python
        for source_type in ["photo", "note", "document", "file"]:
```
to:
```python
        for source_type in ["photo", "document", "file"]:
```

- [ ] **Step 4: Run pipeline tests**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_lightrag_pipeline.py -v`
Expected: PASS (note-related tests removed, others pass)

- [ ] **Step 5: Commit**

```bash
git add niu_api/internal/lightrag_pipeline.py tests/test_lightrag_pipeline.py
git commit -m "feat: remove note source_type from LightRAG pipeline"
```

---

### Task 5: Update phase02 migration tests

**Files:**
- Modify: `tests/test_phase02_lightrag_migration.py` (lines 149-181)

- [ ] **Step 1: Rewrite TestNotesApiUsesLightRAG class**

The old tests reference `sync_note_to_kg` (ainsert-based). Replace with tests for the new `sync_note_to_lightrag` (LightRAGIngester-based).

Replace the entire class (lines 149-181) with:

```python
# ============== 4. notes_api.py → LightRAGIngester ==============


class TestNotesApiUsesLightRAG:
    """notes_api sync_note_to_lightrag should use LightRAGIngester.inject_entity."""

    def test_no_niu_kg_server_import_in_notes_api(self):
        """notes_api.py should NOT import niu_kg_server."""
        import niu_api.notes_api as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "niu_kg_server" not in source

    def test_sync_note_to_lightrag_calls_inject_entity(self):
        """sync_note_to_lightrag should call LightRAGIngester.inject_entity()."""
        from niu_api.notes_api import sync_note_to_lightrag

        with patch("niu_api.internal.lightrag_adapter.LightRAGIngester") as mock_cls:
            mock_ingester = MagicMock()
            mock_cls.return_value = mock_ingester
            mock_ingester.inject_entity.return_value = {"status": "ok"}

            sync_note_to_lightrag("note-1", "Shopping list: milk, eggs", ["shopping"])

            mock_ingester.inject_entity.assert_called_once()
            call_args = mock_ingester.inject_entity.call_args
            assert call_args.kwargs["name"] == "note:note-1"
            assert call_args.kwargs["entity_type"] == "knowledge"

    def test_sync_note_to_lightrag_handles_failure(self):
        """sync_note_to_lightrag should handle LightRAG failure gracefully."""
        from niu_api.notes_api import sync_note_to_lightrag

        with patch("niu_api.internal.lightrag_adapter.LightRAGIngester") as mock_cls:
            mock_ingester = MagicMock()
            mock_cls.return_value = mock_ingester
            mock_ingester.inject_entity.return_value = {"status": "error", "message": "down"}

            # Should not raise, just log warning
            sync_note_to_lightrag("note-1", "test content")

    def test_notes_json_storage_no_sqlite(self):
        """notes.py should NOT use aiosqlite."""
        import niu_api.notes as mod
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "aiosqlite" not in source
        assert "sqlite" not in source.lower()
        assert "notes.db" not in source
```

- [ ] **Step 2: Run migration tests**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_phase02_lightrag_migration.py::TestNotesApiUsesLightRAG -v`
Expected: PASS (4 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/test_phase02_lightrag_migration.py
git commit -m "feat: update notes migration tests for JSON + LightRAGIngester"
```

---

### Task 6: Add notes scanning to SkillSync

**Files:**
- Modify: `agent/injector/sync.py`

- [ ] **Step 1: Add notes scanning method to SkillSync**

Add `_scan_notes()` method and integrate it into `scan_and_sync()`. The method reads `notes.json`, compares with `_last_notes_scan`, and injects new/changed notes into LightRAG as `knowledge` entities.

In `agent/injector/sync.py`, add these changes:

1. Add `import json` at the top (after `import re`).

2. In `SkillSync.__init__`, add:
```python
        self._last_notes_scan: dict[str, str] = {}  # note_id -> content hash
```

3. Add `_scan_notes()` method after `_delete_skill()`:
```python
    def _scan_notes(self):
        """扫描 workspace/notes/notes.json，将变化同步到 LightRAG"""
        ws = os.environ.get("WORKSPACE_PATH", "")
        if not ws:
            return 0, 0
        notes_path = Path(ws) / "notes" / "notes.json"
        if not notes_path.exists():
            return 0, 0

        try:
            notes = json.loads(notes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"[SkillSync] Failed to read notes.json: {e}")
            return 0, 0

        added, updated = 0, 0
        current_ids: set[str] = set()

        for note in notes:
            note_id = note.get("id", "")
            if not note_id:
                continue
            current_ids.add(note_id)

            # Simple content hash for change detection
            content_hash = hash(note.get("content", ""))
            last_hash = self._last_notes_scan.get(note_id)

            if last_hash is None:
                # New note
                self._inject_note_to_lightrag(note_id, note.get("content", ""), note.get("tags", []))
                added += 1
                logger.info(f"[SkillSync] Added note: {note_id}")
            elif last_hash != content_hash:
                # Updated note
                self._inject_note_to_lightrag(note_id, note.get("content", ""), note.get("tags", []))
                updated += 1
                logger.info(f"[SkillSync] Updated note: {note_id}")

            self._last_notes_scan[note_id] = content_hash

        # Detect deleted notes (in last scan but not in current)
        for note_id in list(self._last_notes_scan.keys()):
            if note_id not in current_ids:
                try:
                    from niu_api.internal.lightrag_adapter import LightRAGAdapter
                    adapter = LightRAGAdapter()
                    adapter.delete_entity(f"note:{note_id}")
                    logger.info(f"[SkillSync] Deleted note: {note_id}")
                except Exception as e:
                    logger.warning(f"[SkillSync] Failed to delete note {note_id}: {e}")
                self._last_notes_scan.pop(note_id, None)

        return added, updated

    def _inject_note_to_lightrag(self, note_id: str, content: str, tags: list[str]):
        """注入便签到 LightRAG 知识图谱"""
        try:
            from niu_api.internal.lightrag_adapter import LightRAGIngester

            ingester = LightRAGIngester()
            description = content
            if tags:
                description += f" | 标签: {', '.join(tags)}"

            result = ingester.inject_entity(
                name=f"note:{note_id}",
                entity_type="knowledge",
                description=description,
                source_id=f"note:{note_id}",
                chunk_content=description,
                file_path=f"note://{note_id}",
            )
            if result.get("status") != "ok":
                logger.warning(f"[SkillSync] Note inject failed for {note_id}: {result.get('message', '')}")
        except Exception as e:
            logger.warning(f"[SkillSync] Note inject failed for {note_id}: {e}")
```

4. In `scan_and_sync()`, after the skills scanning loop, add:
```python
        # Scan notes
        try:
            note_added, note_updated = self._scan_notes()
            added += note_added
            updated += note_updated
        except Exception as e:
            logger.error(f"[SkillSync] Notes scan failed: {e}")
```

- [ ] **Step 2: Run SkillSync tests**

Run: `cd E:/tools/ai-bot && python -m pytest tests/ -k "skill" -v`
Expected: PASS (existing SkillSync tests still pass)

- [ ] **Step 3: Commit**

```bash
git add agent/injector/sync.py
git commit -m "feat: add notes scanning to SkillSync"
```

---

### Task 7: Create note-management Skill file

**Files:**
- Create: `memory/skills/note-management.md`

- [ ] **Step 1: Create the Skill file**

```markdown
---
name: note-management
description: Use when user asks to create, read, update, delete, or search sticky notes/便签
---

# 便签管理

便签存储在 workspace 的 `notes/notes.json` 文件中，格式为 JSON 数组。

## 读取所有便签

```bash
cat "$WORKSPACE_PATH/notes/notes.json"
```

## 创建便签

用 jq 追加一条新便签：

```bash
ID="$(date +%s)"
TIME="$(date -Iseconds)"
jq --arg id "$ID" --arg content "便签内容" --arg time "$TIME" \
  '. += [{"id": $id, "content": $content, "tags": [], "created_at": $time, "updated_at": null}]' \
  "$WORKSPACE_PATH/notes/notes.json" > /tmp/notes_tmp.json && mv /tmp/notes_tmp.json "$WORKSPACE_PATH/notes/notes.json"
```

## 删除便签

用 jq 过滤掉指定 ID：

```bash
jq 'del(.[] | select(.id == "目标ID"))' "$WORKSPACE_PATH/notes/notes.json" > /tmp/notes_tmp.json && mv /tmp/notes_tmp.json "$WORKSPACE_PATH/notes/notes.json"
```

## 语义搜索

便签已自动同步到知识图谱（LightRAG），通过正常对话即可检索到相关便签内容。无需手动搜索。

## 注意事项

- `WORKSPACE_PATH` 环境变量由系统自动设置
- 便签文件不存在时表示没有便签，不需要创建空文件
- 修改便签后，SkillSync 会自动将变化同步到知识图谱
```

- [ ] **Step 2: Verify Skill file has proper YAML frontmatter**

Run: `cd E:/tools/ai-bot && python -c "from agent.injector.sync import SkillSync; s = SkillSync.__new__(SkillSync); fm = s._parse_yaml_frontmatter(open('memory/skills/note-management.md').read()); print(fm)"`

Expected: `{'name': 'note-management', 'description': 'Use when user asks to create, read, update, delete, or search sticky notes/便签'}`

- [ ] **Step 3: Commit**

```bash
git add memory/skills/note-management.md
git commit -m "feat: add note-management Skill for Agent bash operations"
```

---

### Task 8: Final cleanup and verification

**Files:**
- Verify: All test suites pass
- Verify: No leftover SQLite references

- [ ] **Step 1: Run full test suite**

Run: `cd E:/tools/ai-bot && python -m pytest tests/ -v --timeout=30`
Expected: All tests pass

- [ ] **Step 2: Check for leftover SQLite/aiosqlite references**

Run: `cd E:/tools/ai-bot && grep -rn "aiosqlite\|notes\.db\|notes_db\|init_db.*notes" niu_api/ agent/ --include="*.py"`
Expected: No matches (all SQLite references removed)

- [ ] **Step 3: Check for leftover `[Note:]` prefix references**

Run: `cd E:/tools/ai-bot && grep -rn "\[Note:" niu_api/ agent/ --include="*.py"`
Expected: No matches in source code (only in docs/comments if any)

- [ ] **Step 4: Verify notes_api.py no longer has sync_note_to_kg**

Run: `cd E:/tools/ai-bot && grep -n "sync_note_to_kg" niu_api/notes_api.py`
Expected: No matches (replaced by `sync_note_to_lightrag`)

- [ ] **Step 5: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore: final cleanup for notes JSON redesign"
```