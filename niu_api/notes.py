"""
Notes Store - JSON-based sticky notes storage

便签数据持久化，JSON 文件位于 {workspace}/notes/notes.json
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


def _get_notes_path() -> str:
    """Return the path to notes.json.

    Workspace from WORKSPACE_PATH env var, fallback ~/.niu/notes/notes.json.
    """
    workspace = os.environ.get("WORKSPACE_PATH")
    if workspace:
        return os.path.join(workspace, "notes", "notes.json")
    home = os.path.expanduser("~")
    return os.path.join(home, ".niu", "notes", "notes.json")


def _ensure_dir() -> None:
    """Create notes directory if missing."""
    notes_path = _get_notes_path()
    notes_dir = os.path.dirname(notes_path)
    os.makedirs(notes_dir, exist_ok=True)


def _atomic_write(data: list) -> None:
    """Write JSON via temp file + rename (Windows-safe).

    On Windows, rename fails if target exists, so delete first.
    """
    notes_path = _get_notes_path()
    _ensure_dir()
    dir_path = os.path.dirname(notes_path)

    fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows: os.rename fails if target exists
        if os.path.exists(notes_path):
            os.remove(notes_path)
        os.rename(tmp_path, notes_path)
    except BaseException:
        # Clean up temp file on any error
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_notes() -> List[Dict]:
    """Read all notes from JSON file.

    Return [] if file missing or corrupt.
    """
    notes_path = _get_notes_path()
    if not os.path.exists(notes_path):
        return []
    try:
        with open(notes_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        logger.warning(f"Notes file has unexpected format, returning empty list: {notes_path}")
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read notes file: {e}")
        return []


def write_notes(notes: List[Dict]) -> None:
    """Atomic write all notes."""
    _atomic_write(notes)


def create_note(note_id: str, content: str, tags: Optional[List[str]] = None, created_at: Optional[str] = None) -> Dict:
    """Append a note. Auto-generate created_at if None.

    Return {"id": note_id, "status": "created"}.
    """
    if created_at is None:
        created_at = datetime.now().isoformat()

    note = {
        "id": note_id,
        "content": content,
        "tags": tags if tags is not None else [],
        "created_at": created_at,
        "updated_at": None,
    }

    notes = read_notes()
    notes.append(note)
    _atomic_write(notes)
    logger.info(f"Note created: {note_id}")
    return {"id": note_id, "status": "created"}


def update_note(note_id: str, content: Optional[str] = None, tags: Optional[List[str]] = None) -> Dict:
    """Update note content/tags + set updated_at.

    Return {"id": note_id, "status": "updated"} or {"id": note_id, "status": "not_found"}.
    """
    notes = read_notes()
    for note in notes:
        if note["id"] == note_id:
            if content is not None:
                note["content"] = content
            if tags is not None:
                note["tags"] = tags
            note["updated_at"] = datetime.now().isoformat()
            _atomic_write(notes)
            logger.info(f"Note updated: {note_id}")
            return {"id": note_id, "status": "updated"}

    return {"id": note_id, "status": "not_found"}


def delete_note(note_id: str) -> Dict:
    """Remove note from JSON + call LightRAGAdapter().delete_entity().

    Return {"id": note_id, "status": "deleted"} or {"id": note_id, "status": "not_found"}.
    """
    notes = read_notes()
    original_len = len(notes)
    notes = [n for n in notes if n["id"] != note_id]

    if len(notes) == original_len:
        return {"id": note_id, "status": "not_found"}

    _atomic_write(notes)
    logger.info(f"Note deleted: {note_id}")

    # Sync deletion to knowledge graph
    try:
        from niu_api.internal.lightrag_adapter import LightRAGAdapter
        adapter = LightRAGAdapter()
        adapter.delete_entity(f"note:{note_id}")
    except Exception as e:
        logger.warning(f"LightRAG delete_entity failed for note:{note_id}: {e}")

    return {"id": note_id, "status": "deleted"}


def list_notes() -> List[Dict]:
    """Return all notes sorted by created_at DESC."""
    notes = read_notes()
    return sorted(notes, key=lambda n: n.get("created_at", ""), reverse=True)


def get_note(note_id: str) -> Optional[Dict]:
    """Return single note dict or None."""
    notes = read_notes()
    for note in notes:
        if note["id"] == note_id:
            return note
    return None


# Backward-compatible init_db for __main__.py (no-op for JSON storage)
async def init_db():
    """No-op initializer for backward compatibility.

    JSON storage creates the directory on first write, so no
    initialization is needed at startup.
    """
    _ensure_dir()
    logger.info(f"Notes JSON storage ready: {_get_notes_path()}")
