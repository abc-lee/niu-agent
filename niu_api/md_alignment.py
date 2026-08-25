"""F1 与 Message.DB 的有界对齐扫描（spec §3.4）。不全库扫描；缺口超限只补最近 N 条。"""

import json
import os

from agent.md_mirror import F1_PATH, append_record, format_message_record
from loguru import logger


def _last_msg_id_of_f1(f1_path: str) -> str | None:
    if not os.path.exists(f1_path):
        return None
    last_id = None
    with open(f1_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith('{"msg_id":'):
                try:
                    last_id = json.loads(line)["msg_id"]
                except Exception:
                    continue
    return last_id


async def align_f1_with_store(store, f1_path: str | None = None, max_backfill: int = 200) -> int:
    """对齐 F1 与 DB，返回补写条数。store 需提供 async get_messages()（rowid 序）。

    已知边界（工程五决策1②，接受）：补写读的是 DB 当前内容——尾失联窗口内若压缩先行，
    被替换消息补入的是 [摘要] 视图记录而非原文；缺口超 max_backfill 的更早历史不补。
    """
    p1 = f1_path or F1_PATH
    messages = await store.get_messages()
    index_by_id = {getattr(m, "id", "") or "": i for i, m in enumerate(messages)}
    last_id = _last_msg_id_of_f1(p1)
    if last_id is None:
        start = max(0, len(messages) - max_backfill)
    else:
        idx = index_by_id.get(last_id)
        if idx is None:
            logger.warning("[MdAlign] F1 尾部 msg_id 不在 DB，跳过对齐")
            return 0
        start = idx + 1
    gap = messages[start:]
    if not gap:
        return 0
    backfill = gap[-max_backfill:] if len(gap) > max_backfill else gap
    ok = 0
    for m in backfill:
        block = format_message_record(
            msg_id=getattr(m, "id", "") or "",
            created_at=getattr(m, "created_at", "") or "",
            role=getattr(m, "role", "") or "",
            content=getattr(m, "content", "") or "",
            tool_calls=getattr(m, "tool_calls", None),
            tool_call_id=getattr(m, "tool_call_id", "") or "",
            degraded_reason=getattr(m, "degraded_reason", "") or "",
        )
        if block and append_record(block, p1):
            ok += 1
    if ok:
        logger.info(f"[MdAlign] F1 对齐补写 {ok}/{len(gap)} 条缺口")
    return ok
