"""上下文组装器（Context Assembler）。

模块：blocks 指针块存储 / slicer 会话单元切割 / calibration token 校准倍率 /
compaction 批量压实。接线点在 context_manager（组装出口 80% 触发）、
runner._on_context_high_usage（真值回调回写）与 compat /compact 端点。
"""

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
    "rowid_range_query",
    "slice_units",
    "summarizer",
    "upsert_blocks",
]
