"""上下文组装器（Context Assembler）。

模块：blocks 指针块存储 / slicer 会话单元切割 / calibration token 校准倍率 /
compaction 批量压实 / integrity 一致性校验重建。接线点在 context_manager
（组装出口 80% 触发）、runner._on_context_high_usage（真值回调回写）、
compat /compact 端点与 /new 清理面（reset_derived_state）。
"""

from pathlib import Path

from loguru import logger

from agent.context_assembler import calibration, compaction, summarizer
from agent.context_assembler.blocks import (
    PointerBlock,
    default_db_path,
    delete_all,
    load_all,
    load_by_ids,
    rowid_range_query,
    upsert_blocks,
)
from agent.context_assembler.slicer import slice_units

__all__ = [
    "PointerBlock",
    "calibration",
    "compaction",
    "default_db_path",
    "delete_all",
    "load_all",
    "load_by_ids",
    "reset_derived_state",
    "rowid_range_query",
    "slice_units",
    "summarizer",
    "upsert_blocks",
]


def reset_derived_state(blocks_db_path=None, calibration_path=None) -> None:
    """/new 清理面（spec §4 / 计划 Task 8）：派生数据全量作废。

    - 指针块库删除（文件级，含 -wal/-shm 副文件；删除失败回退清空表）——
      派生数据可从 messages.db 全量重建
    - token 校准倍率复位安全默认（token_calibration.json 删除 + 进程内缓存作废）
    - 压实滞回闸门闩锁解除 + ContextManager 系统 token 估算归零（内存状态作废）

    F1/F2/F3 截断由 clear_chat 既有 truncate_relay_files 负责；journal.md 本体
    按 §8 拍板保留不清空。
    """
    p = Path(blocks_db_path) if blocks_db_path is not None else default_db_path()
    try:
        for suffix in ("", "-wal", "-shm"):
            q = Path(str(p) + suffix)
            if q.exists():
                q.unlink()
    except OSError as e:
        logger.warning(f"[ContextAssembler] /new 块库删除失败，回退清空表: {e}")
        delete_all(p)
    calibration.reset(calibration_path)
    compaction.AUTO_GATE.release()
    from agent.context_manager import peek_context_manager
    cm = peek_context_manager()
    if cm is not None:
        cm.set_system_token_estimate(0)
