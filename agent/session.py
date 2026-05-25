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
    role: str  # 'user' | 'assistant' | 'system' | 'tool'
    content: str
    tool_calls: List[Dict] = field(default_factory=list)
    tool_results: List[Dict] = field(default_factory=list)
    tool_call_id: str = ""  # Links tool result to assistant's tool_calls[].id
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

            # Migration: add tool_call_id column if missing (compat with existing DBs)
            cursor = await db.execute("PRAGMA table_info(messages)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "tool_call_id" not in columns:
                await db.execute(
                    "ALTER TABLE messages ADD COLUMN tool_call_id TEXT DEFAULT ''"
                )
                await db.commit()
                logger.info("Migrated messages table: added tool_call_id column")

            await db.commit()
            logger.info(f"MessageStore initialized: {self.db_path}")

    async def add_message(
        self,
        role: str,
        content: str,
        tool_calls: List[Dict] = None,
        tool_results: List[Dict] = None,
        tool_call_id: str = "",
    ) -> str:
        """Add a message"""
        msg_id = str(uuid4())
        created_at = datetime.now().isoformat()
        tool_calls_json = json.dumps(tool_calls or [], ensure_ascii=False)
        tool_results_json = json.dumps(tool_results or [], ensure_ascii=False)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO messages
                   (id, role, content, tool_calls, tool_results, tool_call_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, role, content, tool_calls_json, tool_results_json, tool_call_id, created_at),
            )
            await db.commit()

        logger.debug(f"Added message: {msg_id}")
        return msg_id

    def add_message_sync(
        self,
        role: str,
        content: str,
        tool_calls: List[Dict] = None,
        tool_results: List[Dict] = None,
        tool_call_id: str = "",
    ) -> str:
        """同步版本 add_message — 供 executor 线程调用

        使用 sqlite3（同步）+ WAL 模式 + busy_timeout，确保从
        executor 线程安全写入，不会与主 async 事件循环的
        aiosqlite 连接冲突。
        """
        import sqlite3

        msg_id = str(uuid4())
        created_at = datetime.now().isoformat()
        tool_calls_json = json.dumps(tool_calls or [], ensure_ascii=False)
        tool_results_json = json.dumps(tool_results or [], ensure_ascii=False)

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls TEXT,
                    tool_results TEXT,
                    tool_call_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT INTO messages
                   (id, role, content, tool_calls, tool_results, tool_call_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, role, content, tool_calls_json, tool_results_json, tool_call_id, created_at),
            )
            conn.commit()
        finally:
            conn.close()

        logger.debug(f"Added message (sync): {msg_id}")
        return msg_id

    async def get_messages(self, limit: Optional[int] = None, before_id: Optional[str] = None) -> List[Message]:
        """Get messages (chronological order). If limit is None, return all messages.

        Pagination uses created_at timestamp (not UUID) for correct time-order paging.
        before_id is resolved to its created_at, then used for cursor-based pagination.
        """
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if before_id:
                # Resolve before_id to its created_at for time-based pagination
                cursor = await db.execute(
                    "SELECT created_at FROM messages WHERE id = ?",
                    (before_id,),
                )
                before_row = await cursor.fetchone()
                if before_row:
                    before_ts = before_row["created_at"]
                    if limit is not None:
                        cursor = await db.execute(
                            """SELECT * FROM messages
                               WHERE created_at < ?
                               ORDER BY created_at DESC
                               LIMIT ?""",
                            (before_ts, limit),
                        )
                    else:
                        cursor = await db.execute(
                            """SELECT * FROM messages
                               WHERE created_at < ?
                               ORDER BY created_at DESC""",
                            (before_ts,),
                        )
                else:
                    # before_id not found, fall back to no cursor
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
                        tool_call_id=row["tool_call_id"] if "tool_call_id" in row.keys() else "",
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
        """Clear all messages and cleanup referenced temp files"""
        # Single connection + transaction to avoid race condition
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Collect content before deleting
            cursor = await db.execute("SELECT content FROM messages")
            rows = await cursor.fetchall()
            # Count before deleting (rowcount for DELETE is unreliable in aiosqlite)
            cursor = await db.execute("SELECT COUNT(*) FROM messages")
            count = (await cursor.fetchone())[0]
            # Delete all
            await db.execute("DELETE FROM messages")
            await db.commit()

        # Extract temp file paths and cleanup (outside DB transaction)
        tmp_files = _extract_tmp_paths([row["content"] for row in rows if row["content"]])
        cleaned = 0
        if tmp_files:
            from agent.tmp_dir import cleanup_tmp_files
            cleaned = cleanup_tmp_files(tmp_files)

        logger.info(f"Cleared {count} messages, cleaned {cleaned} temp files")
        return count

    async def update_message(self, message_id: str, content: str) -> bool:
        """Update message content by ID. Returns True if updated."""
        # Read old content to cleanup temp file references
        old_content = None
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT content FROM messages WHERE id = ?",
                (message_id,),
            )
            row = await cursor.fetchone()
            if row:
                old_content = row["content"]

            # Update
            cursor = await db.execute(
                "UPDATE messages SET content = ? WHERE id = ?",
                (content, message_id),
            )
            updated = cursor.rowcount
            await db.commit()

        if updated:
            # Cleanup temp files that are no longer referenced
            # (in old content but not in new content)
            if old_content:
                old_tmp = set(_extract_tmp_paths([old_content]))
                new_tmp = set(_extract_tmp_paths([content])) if content else set()
                to_clean = old_tmp - new_tmp
                if to_clean:
                    from agent.tmp_dir import cleanup_tmp_files
                    cleanup_tmp_files(list(to_clean))
            logger.debug(f"Updated message: {message_id}")
        return updated > 0

    async def delete_messages_by_ids(self, message_ids: List[str]) -> dict:
        """Delete messages by IDs and cleanup referenced temp files.
        Returns dict with deleted_count and freed_tokens."""
        if not message_ids:
            return {"deleted_count": 0, "freed_tokens": 0}

        # Single connection + transaction
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            placeholders = ",".join("?" * len(message_ids))
            # Collect content before deleting (for token estimation + temp cleanup)
            cursor = await db.execute(
                f"SELECT role, content FROM messages WHERE id IN ({placeholders})",
                message_ids,
            )
            rows = await cursor.fetchall()
            # Delete
            cursor = await db.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})",
                message_ids,
            )
            deleted = cursor.rowcount
            await db.commit()

        # Estimate freed tokens from deleted content
        freed_tokens = 0
        for row in rows:
            try:
                from litellm import token_counter
                t = token_counter(model="gpt-4o", messages=[{"role": row["role"], "content": row["content"] or ""}])
            except Exception:
                t = max(1, len(row["content"] or "") // 2) + 4
            freed_tokens += t

        # Extract temp file paths and cleanup (outside DB transaction)
        tmp_files = _extract_tmp_paths([row["content"] for row in rows if row["content"]])
        cleaned = 0
        if tmp_files:
            from agent.tmp_dir import cleanup_tmp_files
            cleaned = cleanup_tmp_files(tmp_files)

        logger.info(f"Deleted {deleted} messages by IDs, freed {freed_tokens} tokens, cleaned {cleaned} temp files")
        return {"deleted_count": deleted, "freed_tokens": freed_tokens}


def _extract_tmp_paths(contents: list[str]) -> list[str]:
    """Extract temp file paths from message content strings.

    Looks for paths that contain /.niu/tmp/ or \\.niu\\tmp\\
    """
    import re
    from agent.tmp_dir import is_tmp_file

    paths = []
    for content in contents:
        if not content:
            continue
        # Match file paths (Windows or Unix style)
        # Pattern: drive letter or /home + path containing .niu/tmp
        found = re.findall(
            r'(?:[A-Za-z]:[/\\]|/)[^\s"\'<>]+[/\\]\.niu[/\\]tmp[/\\][^\s"\'<>]+',
            content,
        )
        for p in found:
            if is_tmp_file(p):
                paths.append(p)
    return paths


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
