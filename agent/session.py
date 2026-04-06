"""
Message Store - SQLite-based message storage

Simplified architecture:
- No session concept, all messages belong to user
- Single messages table
- Sub-agents are independent, no session needed
"""

import os
import json
import asyncio
import aiosqlite
from datetime import datetime
from uuid import uuid4
from typing import Optional, List, Dict
from dataclasses import dataclass, field, asdict
from loguru import logger


@dataclass
class Message:
    """A chat message"""

    id: str
    role: str  # 'user' | 'assistant' | 'system'
    content: str
    tool_calls: List[Dict] = field(default_factory=list)
    tool_results: List[Dict] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class MessageStore:
    """
    SQLite-based message store
    
    Simplified design:
    - No session concept
    - All messages belong to user
    - Full history retrieval
    """

    def __init__(self, db_path: str = None):
        if db_path is None:
            home = os.path.expanduser("~")
            niu_dir = os.path.join(home, ".niu")
            os.makedirs(niu_dir, exist_ok=True)
            db_path = os.path.join(niu_dir, "messages.db")

        self.db_path = db_path
        self._db = None

    async def init_db(self):
        """Initialize database schema"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_results TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_created_at 
                ON messages(created_at ASC)
            """)

            await db.commit()
            logger.info(f"MessageStore initialized: {self.db_path}")

    async def add_message(
        self,
        role: str,
        content: str,
        tool_calls: List[Dict] = None,
        tool_results: List[Dict] = None,
    ) -> str:
        """Add a message"""
        msg_id = str(uuid4())
        created_at = datetime.now().isoformat()
        tool_calls_json = json.dumps(tool_calls or [], ensure_ascii=False)
        tool_results_json = json.dumps(tool_results or [], ensure_ascii=False)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO messages 
                   (id, role, content, tool_calls, tool_results, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (msg_id, role, content, tool_calls_json, tool_results_json, created_at),
            )
            await db.commit()

        logger.debug(f"Added message: {msg_id}")
        return msg_id

    async def get_messages(self, limit: Optional[int] = None, before_id: Optional[str] = None) -> List[Message]:
        """Get messages (chronological order). If limit is None, return all messages."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if before_id:
                if limit is not None:
                    cursor = await db.execute(
                        """SELECT * FROM messages
                           WHERE id < ?
                           ORDER BY created_at DESC
                           LIMIT ?""",
                        (before_id, limit),
                    )
                else:
                    cursor = await db.execute(
                        """SELECT * FROM messages
                           WHERE id < ?
                           ORDER BY created_at DESC""",
                        (before_id,),
                    )
            else:
                if limit is not None:
                    cursor = await db.execute(
                        """SELECT * FROM messages
                           ORDER BY created_at DESC
                           LIMIT ?""",
                        (limit,),
                    )
                else:
                    cursor = await db.execute(
                        """SELECT * FROM messages
                           ORDER BY created_at DESC"""
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
                        created_at=row["created_at"],
                    )
                )

            return messages

    async def count_messages(self) -> int:
        """Count total messages"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM messages")
            result = await cursor.fetchone()
            return result[0] if result else 0

    async def clear_messages(self) -> int:
        """Clear all messages (for /new command)"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("DELETE FROM messages")
            deleted = cursor.rowcount
            await db.commit()

        logger.info(f"Cleared {deleted} messages")
        return deleted

    async def delete_messages_by_ids(self, message_ids: List[str]) -> int:
        """Delete messages by IDs"""
        if not message_ids:
            return 0

        async with aiosqlite.connect(self.db_path) as db:
            placeholders = ",".join("?" * len(message_ids))
            cursor = await db.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})",
                message_ids,
            )
            deleted = cursor.rowcount
            await db.commit()

        logger.info(f"Deleted {deleted} messages by IDs")
        return deleted


# Global instance
_message_store: Optional[MessageStore] = None


async def get_message_store() -> MessageStore:
    """Get or create global message store"""
    global _message_store
    if _message_store is None:
        _message_store = MessageStore()
        await _message_store.init_db()
    return _message_store


# Backward compatibility alias
SessionStore = MessageStore
get_session_store = get_message_store
