"""
Notes Store - SQLite-based sticky notes storage

便利贴数据持久化，数据库位于 ~/.niu/notes.db
"""

import os
import aiosqlite
from datetime import datetime
from typing import Optional, List, Dict
from loguru import logger


_db_path: Optional[str] = None


def _get_db_path() -> str:
    global _db_path
    if _db_path is None:
        home = os.path.expanduser("~")
        niu_dir = os.path.join(home, ".niu")
        os.makedirs(niu_dir, exist_ok=True)
        _db_path = os.path.join(niu_dir, "notes.db")
    return _db_path


async def init_db():
    """Initialize notes database"""
    db_path = _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT
            )
        """)
        await db.commit()
    logger.info(f"Notes DB initialized: {db_path}")


async def create_note(note_id: str, content: str, created_at: str = None) -> Dict:
    """Create a new note"""
    if created_at is None:
        created_at = datetime.now().isoformat()

    db_path = _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO notes (id, content, created_at) VALUES (?, ?, ?)",
            (note_id, content, created_at)
        )
        await db.commit()

    return {"id": note_id, "status": "created"}


async def update_note(note_id: str, content: str) -> Dict:
    """Update an existing note"""
    updated_at = datetime.now().isoformat()

    db_path = _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "UPDATE notes SET content = ?, updated_at = ? WHERE id = ?",
            (content, updated_at, note_id)
        )
        await db.commit()
        if cursor.rowcount == 0:
            return {"id": note_id, "status": "not_found"}

    return {"id": note_id, "status": "updated"}


async def delete_note(note_id: str) -> Dict:
    """Delete a note"""
    db_path = _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await db.commit()
        if cursor.rowcount == 0:
            return {"id": note_id, "status": "not_found"}

    return {"id": note_id, "status": "deleted"}


async def list_notes() -> List[Dict]:
    """List all notes"""
    db_path = _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, content, created_at, updated_at FROM notes ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()

    return [
        {
            "id": row["id"],
            "content": row["content"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        for row in rows
    ]


async def get_note(note_id: str) -> Optional[Dict]:
    """Get a single note by ID"""
    db_path = _get_db_path()
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, content, created_at, updated_at FROM notes WHERE id = ?",
            (note_id,)
        )
        row = await cursor.fetchone()

    if row is None:
        return None

    return {
        "id": row["id"],
        "content": row["content"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }
