"""
Message Store - SQLite-based message storage

Simplified architecture:
- No session concept, all messages belong to user
- Single messages table
- Sub-agents are independent, no session needed
"""

import json
import os
from datetime import datetime
from uuid import uuid4

import aiosqlite


def _safe_json(raw, default=None):
    """Parse JSON from DB column, returning default on any failure."""
    if default is None:
        default = []
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return default
from dataclasses import asdict, dataclass, field  # noqa: E402

from loguru import logger  # noqa: E402


@dataclass
class Message:
    """A chat message"""

    id: str
    role: str  # 'user' | 'assistant' | 'system' | 'tool' | 'subagent_msg'
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    tool_call_id: str = ""  # Links tool result to assistant's tool_calls[].id
    degraded_reason: str = ""  # E4-12：降级回复错误类别（"timeout"|"internal"——旧行 NULL 读取端容错）
    created_at: str = ""
    rowid: int = 0  # SQLite rowid, 0 = sentinel for "not loaded from DB" (real rowid starts at 1)

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
            # V4: WAL模式 + busy_timeout，支持高频并发写入
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")

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

            # DEPRECATED: no longer used for ORDER BY (switched to rowid), kept for existing DBs
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

            # Migration: add degraded_reason column if missing (E4-12——降级回复可追溯标记；
            # 旧行 NULL——读取端 .get 默认容错；显式列清单/显式 SELECT 列——不迁移旧库即查询失败)
            cursor = await db.execute("PRAGMA table_info(messages)")
            columns = [row[1] for row in await cursor.fetchall()]
            if "degraded_reason" not in columns:
                await db.execute(
                    "ALTER TABLE messages ADD COLUMN degraded_reason TEXT"
                )
                await db.commit()
                logger.info("Migrated messages table: added degraded_reason column")

            await db.commit()
            logger.info(f"MessageStore initialized: {self.db_path}")

    async def add_message(
        self,
        role: str,
        content: str,
        tool_calls: list[dict] = None,
        tool_results: list[dict] = None,
        tool_call_id: str = "",
        degraded_reason: str = "",
    ) -> str:
        """Add a message"""
        msg_id = str(uuid4())
        created_at = datetime.now().isoformat()
        tool_calls_json = json.dumps(tool_calls or [], ensure_ascii=False)
        tool_results_json = json.dumps(tool_results or [], ensure_ascii=False)

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO messages
                   (id, role, content, tool_calls, tool_results, tool_call_id, degraded_reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, role, content, tool_calls_json, tool_results_json, tool_call_id, degraded_reason, created_at),
            )
            await db.commit()

        logger.debug(f"Added message: {msg_id}")
        return msg_id

    async def get_messages(self, limit: int | None = None, before_id: str | None = None) -> list[Message]:
        """Get messages (chronological order by write sequence). If limit is None, return all messages.

        Pagination uses rowid (write order), not created_at timestamp.
        before_id is resolved to its rowid for cursor-based pagination.
        """
        _columns = "id, role, content, tool_calls, tool_results, tool_call_id, degraded_reason, created_at, rowid"

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
                            f"""SELECT {_columns} FROM messages
                               WHERE rowid < ?
                               ORDER BY rowid DESC
                               LIMIT ?""",
                            (before_rowid, limit),
                        )
                    else:
                        cursor = await db.execute(
                            f"""SELECT {_columns} FROM messages
                               WHERE rowid < ?
                               ORDER BY rowid DESC""",
                            (before_rowid,),
                        )
                else:
                    # before_id not found, fall back to no cursor
                    if limit is not None:
                        cursor = await db.execute(
                            f"""SELECT {_columns} FROM messages
                               ORDER BY rowid DESC
                               LIMIT ?""",
                            (limit,),
                        )
                    else:
                        cursor = await db.execute(
                            f"""SELECT {_columns} FROM messages
                               ORDER BY rowid DESC"""
                        )
            else:
                if limit is not None:
                    cursor = await db.execute(
                        f"""SELECT {_columns} FROM messages
                           ORDER BY rowid DESC
                           LIMIT ?""",
                        (limit,),
                    )
                else:
                    cursor = await db.execute(
                        f"""SELECT {_columns} FROM messages
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
                        tool_calls=_safe_json(row["tool_calls"]),
                        tool_results=_safe_json(row["tool_results"]),
                        tool_call_id=row["tool_call_id"] if "tool_call_id" in row.keys() else "",
                        degraded_reason=row["degraded_reason"] if "degraded_reason" in row.keys() else "",
                        created_at=row["created_at"],
                        rowid=row["rowid"],
                    )
                )

            return messages

    async def count_messages(self) -> int:
        """Count total messages"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM messages")
            result = await cursor.fetchone()
            return result[0] if result else 0

    async def get_max_rowid(self) -> int:
        """获取 messages 表的最大 rowid，空表返回 0"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT MAX(rowid) FROM messages")
            result = await cursor.fetchone()
            return result[0] if result and result[0] is not None else 0

    async def get_assistant_text_after_rowid(self, after_rowid: int) -> list[tuple[int, str]]:
        """获取指定 rowid 之后的 assistant 文本消息，返回 [(rowid, content)]"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT rowid, content FROM messages
                   WHERE rowid > ? AND role = 'assistant' AND content IS NOT NULL AND content != ''
                   ORDER BY rowid ASC""",
                (after_rowid,),
            )
            return [(row[0], row[1]) for row in await cursor.fetchall()]

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

    async def update_message(self, message_id: str, content: str, clear_tool_calls: bool = False) -> bool:
        """Update message content by ID. Returns True if updated.

        Args:
            clear_tool_calls: If True, also clear the tool_calls field (for compression
                that replaces assistant(tool_calls) content with summary text).
        """
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

            # Update content + optionally clear tool_calls
            if clear_tool_calls:
                cursor = await db.execute(
                    "UPDATE messages SET content = ?, tool_calls = '[]' WHERE id = ?",
                    (content, message_id),
                )
            else:
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

    async def delete_messages_by_ids(self, message_ids: list[str]) -> dict:
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
                f"SELECT role, content, tool_calls FROM messages WHERE id IN ({placeholders})",
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
                from agent.token_calculator import TokenCalculator
                calc = TokenCalculator.get()
                tc_raw = row.get("tool_calls")
                tc = _safe_json(tc_raw, default=None)
                t = calc.count_message_single(row["role"], row["content"] or "", tool_calls=tc)
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
_message_store: MessageStore | None = None


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
