"""指针块一致性校验与整库重建（spec §3.5 / 计划 Task 8）——挂 lifespan Phase1 同段。

校验项（全部纯机械、零 LLM、同步 sqlite3 直查）：
  ① 块端点 start/end msg_id 在 messages.db 中存在
  ② 端点 msg_id 与 rowid 对齐（msg_id 实查 rowid == 块记录 rowid）——
     messages 表无 AUTOINCREMENT，删行后 rowid 可被复用，仅查存在性会漏判
  ③ rowid 区间按块 id 升序单调不重叠（start<=end 且下一块 start > 上一块 end）
  ④ count 与 messages.db [start_rowid, end_rowid] 区间实际行数一致
  ⑤ 块库文件本身可读（损坏文件按不一致处理，走整库重建）

语义铁律（lightrag_manager.run_resilience_phase1 先例——防 launcher 闩锁误触）：
  检测失败 ≠ 损坏——校验过程自身抛异常 → ok=True + check_failed=True + error，
  绝不触发重建；确证不一致才重建（读 messages.db 全量 → slice_units →
  窗口装填 → archive_excluded_units 归档同源逻辑 → 覆盖写块表）。
"""

from __future__ import annotations

import bisect
import sqlite3
from pathlib import Path

from loguru import logger

from agent.context_assembler.blocks import (
    BUSY_TIMEOUT_MS,
    default_db_path,
    delete_all,
    load_all,
)
from agent.context_assembler.slicer import slice_units
from agent.session import Message


def default_messages_db_path() -> Path:
    """messages.db 默认路径（与 agent.session.MessageStore 同位置）。"""
    return Path.home() / ".niu" / "messages.db"


# ---------------------------------------------------------------------------
# messages.db 只读直查（同步；lifespan 启动段无事件循环内 async store 可用性约束）
# ---------------------------------------------------------------------------

def _connect_messages(messages_db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(messages_db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def _read_message_rows(messages_db_path: Path) -> list[tuple[int, str]]:
    """读 messages.db 全量 (rowid, id)，按 rowid 升序。文件不存在/无表 → 空列表。"""
    if not messages_db_path.exists():
        return []
    conn = _connect_messages(messages_db_path)
    try:
        rows = conn.execute("SELECT rowid, id FROM messages ORDER BY rowid").fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []  # 新库未建表 = 无消息，不是损坏
        raise
    finally:
        conn.close()
    return [(int(r[0]), str(r[1])) for r in rows]


def _load_messages(messages_db_path: Path) -> list[Message]:
    """读 messages.db 全量消息（重建用），按 rowid 升序。文件不存在/无表 → 空列表。"""
    if not messages_db_path.exists():
        return []
    conn = _connect_messages(messages_db_path)
    try:
        rows = conn.execute(
            "SELECT id, role, content, created_at, rowid FROM messages ORDER BY rowid"
        ).fetchall()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise
    finally:
        conn.close()
    return [
        Message(
            id=str(r[0]),
            role=str(r[1]),
            content=r[2] or "",
            created_at=r[3] or "",
            rowid=int(r[4]),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 检测
# ---------------------------------------------------------------------------

def _probe_blocks_db(blocks_db_path: Path) -> None:
    """探测块库可读性：文件损坏抛 sqlite3.DatabaseError；无表（新库）视为正常。"""
    conn = sqlite3.connect(str(blocks_db_path), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_blocks'"
        ).fetchone()
        if has_table:
            conn.execute("SELECT COUNT(*) FROM context_blocks").fetchone()
    finally:
        conn.close()


def _detect_issues(blocks_db_path: Path, messages_db_path: Path) -> list[str]:
    """检测全部不一致项。块库文件损坏直接返回单项 issue（走删文件重建）。"""
    if blocks_db_path.exists():
        try:
            _probe_blocks_db(blocks_db_path)
        except sqlite3.DatabaseError:
            return [f"blocks_db_corrupt: {blocks_db_path.name} 文件不可读"]

    blocks = load_all(blocks_db_path)
    if not blocks:
        return []

    rows = _read_message_rows(messages_db_path)
    rowids = [rid for rid, _ in rows]  # 已按 rowid 升序
    id_to_rowid = {mid: rid for rid, mid in rows}

    issues: list[str] = []
    prev_end = 0
    for b in blocks:
        # ① 端点 msg_id 存在性 + ② msg_id↔rowid 对齐
        start_actual = id_to_rowid.get(b.start_msg_id)
        if start_actual is None:
            issues.append(f"块#{b.id} start_msg_id {b.start_msg_id} 不存在于 messages.db")
        elif start_actual != b.start_rowid:
            issues.append(
                f"块#{b.id} start_msg_id↔rowid 不对齐: 记录 {b.start_rowid} 实查 {start_actual}"
            )
        end_actual = id_to_rowid.get(b.end_msg_id)
        if end_actual is None:
            issues.append(f"块#{b.id} end_msg_id {b.end_msg_id} 不存在于 messages.db")
        elif end_actual != b.end_rowid:
            issues.append(
                f"块#{b.id} end_msg_id↔rowid 不对齐: 记录 {b.end_rowid} 实查 {end_actual}"
            )
        # ③ rowid 区间单调不重叠
        if b.start_rowid > b.end_rowid:
            issues.append(f"块#{b.id} 区间倒置: [{b.start_rowid}, {b.end_rowid}]")
        elif b.start_rowid <= prev_end:
            issues.append(
                f"块#{b.id} 区间重叠/非单调: start_rowid {b.start_rowid} <= 前块 end_rowid {prev_end}"
            )
        prev_end = max(prev_end, b.end_rowid)
        # ④ count 与区间实际行数一致
        lo = bisect.bisect_left(rowids, b.start_rowid)
        hi = bisect.bisect_right(rowids, b.end_rowid)
        actual = hi - lo
        if actual != b.count:
            issues.append(f"块#{b.id} count={b.count} 与区间实际行数 {actual} 不一致")
    return issues


# ---------------------------------------------------------------------------
# 整库重建
# ---------------------------------------------------------------------------

def _rebuild(blocks_db_path: Path, messages_db_path: Path, *,
             context_window_tokens: int | None = None) -> int:
    """整库重建：清算块表 → 全量重切 → 窗口外单元归档。返回重建后的块数。

    归档复用 compaction.archive_excluded_units（compact_now 同源逻辑）；块库文件
    损坏时 delete_all 失败 → 删文件（含 -wal/-shm 副文件）后重建。
    窗口装填与组装出口共用 ContextManager.compute_window_start 唯一实现。
    """
    from agent.context_assembler.compaction import archive_excluded_units
    from agent.context_manager import ContextManager, _WINDOW_BUDGET_RATIO

    if not delete_all(blocks_db_path):
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(blocks_db_path) + suffix)
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                logger.warning(f"[BlocksIntegrity] 删除损坏块库文件失败 {p}: {e}")

    messages = _load_messages(messages_db_path)
    if not messages:
        return 0
    units = slice_units(messages)
    if context_window_tokens is None:
        from agent.subagent import _read_context_window_tokens
        context_window_tokens = _read_context_window_tokens()
    budget = int(context_window_tokens * _WINDOW_BUDGET_RATIO)
    converted = [ContextManager._message_to_dict(m) for m in messages]
    window_start = ContextManager.compute_window_start(
        converted, units, budget, ContextManager.count_tokens_simple
    )
    # collect_entities=False：lifespan 段 LightRAG 尚未 eager init，get_lightrag
    # 会触发阻塞式懒初始化——自愈路径不得阻塞启动；实体标签为可选增强，空标签降级。
    archive_excluded_units(messages, units, window_start, blocks_db_path,
                           collect_entities=False)
    return len(load_all(blocks_db_path))


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------

def check_blocks_integrity(blocks_db_path=None, messages_db_path=None, *,
                           context_window_tokens: int | None = None) -> dict:
    """校验指针块与 messages.db 一致性；确证不一致时自动整库重建。

    Args:
        blocks_db_path: 块库路径（默认 ~/.niu/context_blocks.db）
        messages_db_path: messages.db 路径（默认 ~/.niu/messages.db）
        context_window_tokens: 重建窗口预算基数（默认读用户配置 contextWindowSize）

    Returns:
        正常：{"ok": True, "issues": [], "repaired": False}
        重建成功：{"ok": True, "issues": [...], "repaired": True}
        重建失败：{"ok": False, "issues": [...], "repaired": False, "error": str}
        检测失败：{"ok": True, "check_failed": True, "error": str, "issues": [],
                  "repaired": False}——检测失败 ≠ 损坏，绝不触发重建
                  （防 launcher 闩锁误触，run_resilience_phase1 先例）。
    """
    bpath = Path(blocks_db_path) if blocks_db_path is not None else default_db_path()
    mpath = (
        Path(messages_db_path)
        if messages_db_path is not None
        else default_messages_db_path()
    )
    try:
        issues = _detect_issues(bpath, mpath)
    except Exception as e:
        logger.warning(f"[BlocksIntegrity] 一致性检测失败（不视为损坏、不影响启动）: {e}")
        return {
            "ok": True,
            "check_failed": True,
            "error": str(e),
            "issues": [],
            "repaired": False,
        }
    if not issues:
        return {"ok": True, "issues": [], "repaired": False}

    logger.warning(f"[BlocksIntegrity] 检测到 {len(issues)} 项不一致，整库重建: {issues}")
    try:
        n_blocks = _rebuild(bpath, mpath, context_window_tokens=context_window_tokens)
    except Exception as e:
        logger.error(f"[BlocksIntegrity] 整库重建失败: {e}")
        return {"ok": False, "issues": issues, "repaired": False, "error": str(e)}
    logger.info(f"[BlocksIntegrity] 整库重建完成：{n_blocks} 块")
    return {"ok": True, "issues": issues, "repaired": True}
