"""上下文组装器（Context Assembler）——指针块存储层与会话单元切割器。

本包当前仅含纯函数与独立存储层，不含任何接线（不 import 进 runner/compat）。
后续任务（组装器替换/校准倍率/压实）逐步落位于此。
"""

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
    "default_db_path",
    "delete_all",
    "load_all",
    "load_by_ids",
    "rowid_range_query",
    "slice_units",
    "upsert_blocks",
]
