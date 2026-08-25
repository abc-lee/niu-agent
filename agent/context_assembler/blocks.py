"""指针块存储层——会话单元指针块的 SQLite 单表持久化。

存储形态（spec §8 拍板）：SQLite 单表 `~/.niu/context_blocks.db`，
WAL + busy_timeout=5000；写操作走 flock 文件锁（与游标写入同纪律）。

flock 纪律（fcntl-flock-nested-lock-deadlock 教训）：同进程嵌套 flock 同一
.lock 文件会自死锁（不同 fd 各自 open）——本模块所有写操作统一经由
`_block_lock` 单一 helper，helper 内部严禁再调任何加锁函数。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

DB_NAME = "context_blocks.db"
BUSY_TIMEOUT_MS = 5000

SUMMARY_PENDING = "pending"
SUMMARY_DONE = "done"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS context_blocks (
    id INTEGER PRIMARY KEY,
    start_msg_id TEXT NOT NULL,
    end_msg_id TEXT NOT NULL,
    start_rowid INTEGER NOT NULL,
    end_rowid INTEGER NOT NULL,
    count INTEGER NOT NULL,
    time_start TEXT NOT NULL DEFAULT '',
    time_end TEXT NOT NULL DEFAULT '',
    entities TEXT NOT NULL DEFAULT '[]',
    first_user TEXT NOT NULL DEFAULT '',
    summary TEXT,
    summary_state TEXT NOT NULL DEFAULT 'pending',
    session TEXT NOT NULL DEFAULT 'default'
)
"""


def default_db_path() -> Path:
    """默认 DB 路径 ~/.niu/context_blocks.db。"""
    return Path.home() / ".niu" / DB_NAME


@dataclass
class PointerBlock:
    """指针块 schema（spec §3.2 字段全集，含 summary_state + session 预留列）。"""

    id: int
    start_msg_id: str
    end_msg_id: str
    start_rowid: int
    end_rowid: int
    count: int
    time_start: str = ""
    time_end: str = ""
    entities: list[str] = field(default_factory=list)
    first_user: str = ""  # ≤40 字，超长自动截断
    summary: str | None = None
    summary_state: str = SUMMARY_PENDING  # "pending" | "done"
    session: str = "default"  # session 维度预留列

    def __post_init__(self) -> None:
        if len(self.first_user) > 40:
            self.first_user = self.first_user[:40]


# ---------------------------------------------------------------------------
# flock helper（唯一加锁入口）
# ---------------------------------------------------------------------------

def _flock(lock_f) -> None:
    """跨平台排它锁。Unix 用 fcntl.flock，Windows 用 msvcrt.locking。"""
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(lock_f.fileno(), msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(lock_f, fcntl.LOCK_EX)


def _funlock(lock_f) -> None:
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # 解锁失败不影响主流程（进程退出即释放）
    else:
        import fcntl

        fcntl.flock(lock_f, fcntl.LOCK_UN)


@contextmanager
def _block_lock(db_path: Path):
    """写操作统一文件锁。锁文件与 DB 同目录：context_blocks.db.lock。"""
    lock_path = db_path.with_suffix(db_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_f:
        _flock(lock_f)
        try:
            yield
        finally:
            _funlock(lock_f)


# ---------------------------------------------------------------------------
# 连接与容错
# ---------------------------------------------------------------------------

def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_TABLE_SQL)


def _row_to_block(row: tuple) -> PointerBlock:
    (
        bid,
        start_msg_id,
        end_msg_id,
        start_rowid,
        end_rowid,
        count,
        time_start,
        time_end,
        entities_json,
        first_user,
        summary,
        summary_state,
        session,
    ) = row
    entities = []
    try:
        parsed = json.loads(entities_json)
        if isinstance(parsed, list):
            entities = [str(e) for e in parsed]
    except (json.JSONDecodeError, TypeError):
        pass  # 损坏的 entities 列降级为空标签
    return PointerBlock(
        id=bid,
        start_msg_id=start_msg_id,
        end_msg_id=end_msg_id,
        start_rowid=start_rowid,
        end_rowid=end_rowid,
        count=count,
        time_start=time_start,
        time_end=time_end,
        entities=entities,
        first_user=first_user,
        summary=summary,
        summary_state=summary_state,
        session=session,
    )


_BLOCK_COLUMNS = (
    "id, start_msg_id, end_msg_id, start_rowid, end_rowid, count, "
    "time_start, time_end, entities, first_user, summary, summary_state, session"
)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def upsert_blocks(blocks: list[PointerBlock], db_path: Path | None = None) -> int:
    """批量写入/覆盖指针块（按 id 幂等）。返回成功写入条数；DB 损坏时 warning 并返回 0。"""
    if not blocks:
        return 0
    path = Path(db_path) if db_path is not None else default_db_path()
    rows = [
        (
            b.id,
            b.start_msg_id,
            b.end_msg_id,
            b.start_rowid,
            b.end_rowid,
            b.count,
            b.time_start,
            b.time_end,
            json.dumps(b.entities, ensure_ascii=False),
            b.first_user,
            b.summary,
            b.summary_state,
            b.session,
        )
        for b in blocks
    ]
    try:
        with _block_lock(path):
            conn = _connect(path)
            try:
                _ensure_table(conn)
                conn.executemany(
                    f"INSERT OR REPLACE INTO context_blocks ({_BLOCK_COLUMNS}) "
                    f"VALUES ({','.join('?' * 13)})",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning(f"[context_blocks] 写入失败（DB 可能损坏）：{e}")
        return 0
    return len(rows)


def load_all(db_path: Path | None = None) -> list[PointerBlock]:
    """读取全部指针块（按 id 升序）。DB 损坏/不存在时返回空列表并 warning。"""
    path = Path(db_path) if db_path is not None else default_db_path()
    if not path.exists():
        return []
    try:
        conn = _connect(path)
        try:
            _ensure_table(conn)
            rows = conn.execute(
                f"SELECT {_BLOCK_COLUMNS} FROM context_blocks ORDER BY id ASC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning(f"[context_blocks] 读取失败（DB 可能损坏），返回空表：{e}")
        return []
    return [_row_to_block(r) for r in rows]


def load_by_ids(ids: list[int], db_path: Path | None = None) -> list[PointerBlock]:
    """按块 id 集合读取，保持入参顺序；不存在的 id 跳过。"""
    if not ids:
        return []
    by_id = {b.id: b for b in load_all(db_path)}
    return [by_id[i] for i in ids if i in by_id]


def delete_all(db_path: Path | None = None) -> bool:
    """清空全部指针块（保留表结构）。用于全量重建前的清算。"""
    path = Path(db_path) if db_path is not None else default_db_path()
    if not path.exists():
        return True
    try:
        with _block_lock(path):
            conn = _connect(path)
            try:
                _ensure_table(conn)
                conn.execute("DELETE FROM context_blocks")
                conn.commit()
            finally:
                conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning(f"[context_blocks] 清空失败（DB 可能损坏）：{e}")
        return False
    return True


def rowid_range_query(
    start_rowid: int, end_rowid: int, db_path: Path | None = None
) -> list[PointerBlock]:
    """查询 rowid 区间 [start_rowid, end_rowid] 有重叠的块（按 id 升序）。

    重叠判定：block.start_rowid <= end_rowid AND block.end_rowid >= start_rowid。
    """
    if end_rowid < start_rowid:
        return []
    path = Path(db_path) if db_path is not None else default_db_path()
    if not path.exists():
        return []
    try:
        conn = _connect(path)
        try:
            _ensure_table(conn)
            rows = conn.execute(
                f"SELECT {_BLOCK_COLUMNS} FROM context_blocks "
                f"WHERE start_rowid <= ? AND end_rowid >= ? ORDER BY id ASC",
                (end_rowid, start_rowid),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning(f"[context_blocks] 区间查询失败（DB 可能损坏），返回空表：{e}")
        return []
    return [_row_to_block(r) for r in rows]

def query_by_msg_id(msg_id: str, db_path: Path | None = None) -> list[PointerBlock]:
    """查询消息 msg_id 落入 [start_msg_id, end_msg_id] 区间的块（按 id 升序）。

    取舍：SQL 字符串比较 vs 全表载入后 Python 过滤——两者判定语义完全一致
    （同一字符串区间比较），选 SQL 是沿用 rowid_range_query 同模式、避免块多时
    整表载入；若未来 msg_id 方案需自定义排序语义，只需改这一处 WHERE。
    """
    if not msg_id:
        return []
    path = Path(db_path) if db_path is not None else default_db_path()
    if not path.exists():
        return []
    try:
        conn = _connect(path)
        try:
            _ensure_table(conn)
            rows = conn.execute(
                f"SELECT {_BLOCK_COLUMNS} FROM context_blocks "
                f"WHERE start_msg_id <= ? AND end_msg_id >= ? ORDER BY id ASC",
                (msg_id, msg_id),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        logger.warning(f"[context_blocks] msg_id 查询失败（DB 可能损坏），返回空表：{e}")
        return []
    return [_row_to_block(r) for r in rows]
