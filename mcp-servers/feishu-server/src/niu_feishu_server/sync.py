"""飞书日历同步 — 定时任务 ↔ 飞书日历事件

将定时任务同步为飞书日历事件，支持创建、更新、取消。
所有函数返回 dict。
"""

import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from loguru import logger

from niu_feishu_server.client import (
    feishu_calendar_create,
    feishu_calendar_cancel,
    feishu_calendar_update,
)
from niu_feishu_server.converter import cron_to_rrule


# ============== Mapping Store ==============

MAPPING_PATH = Path.home() / ".niu" / "feishu_task_mapping.json"


def _load_mapping() -> dict:
    """加载 task_name → event_id 映射"""
    if not MAPPING_PATH.exists():
        return {}
    try:
        return json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_mapping(mapping: dict):
    """保存 task_name → event_id 映射"""
    MAPPING_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAPPING_PATH.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


# ============== Time Helpers ==============

def _next_run_from_cron(cron: str) -> tuple[str, str]:
    """从 cron 表达式推算下一次执行时间，返回 (start_iso, end_iso)。

    事件持续 30 分钟。无时区信息统一假设 UTC。
    简单推算：当前时间 + 1 分钟作为下次执行时间（飞书会根据 RRULE 自动调整）。
    """
    now = datetime.now(timezone.utc)
    next_run = now + timedelta(minutes=1)
    start = next_run.isoformat(timespec="seconds")
    end = (next_run + timedelta(minutes=30)).isoformat(timespec="seconds")
    return start, end


# ============== Sync Functions ==============

def sync_task_to_feishu(*, task_name: str, cron: str, prompt: str) -> dict:
    """将定时任务同步到飞书日历。

    Returns dict with ``event_id`` on success, or ``error`` on failure.
    """
    mapping = _load_mapping()

    # 如果已有映射，更新而非重复创建
    existing_event_id = mapping.get(task_name)
    if existing_event_id:
        start, end = _next_run_from_cron(cron)
        rrule = cron_to_rrule(cron)
        result = feishu_calendar_update(
            event_id=existing_event_id,
            summary=f"[定时任务] {task_name}",
            start_time=start,
            end_time=end,
            recurrence=rrule,
            description=prompt,
        )
        if "error" in result:
            logger.warning(f"[Feishu] Task sync update failed: {result['error']}")
        return result

    # 创建新事件
    start, end = _next_run_from_cron(cron)
    rrule = cron_to_rrule(cron)

    result = feishu_calendar_create(
        summary=f"[定时任务] {task_name}",
        start_time=start,
        end_time=end,
        recurrence=rrule,
        description=prompt,
    )

    if "event_id" in result:
        mapping[task_name] = result["event_id"]
        _save_mapping(mapping)
        logger.info(f"[Feishu] Task synced: {task_name} → {result['event_id']}")

    return result


def cancel_feishu_event(*, task_name: str) -> dict:
    """取消飞书日历上的定时任务事件。

    Returns dict with ``success: True`` on success, or ``error`` on failure.
    """
    mapping = _load_mapping()
    event_id = mapping.get(task_name)

    if not event_id:
        logger.debug(f"[Feishu] No mapping for task: {task_name}")
        return {"success": True, "note": "no event found"}

    result = feishu_calendar_cancel(event_id=event_id)

    if "error" not in result:
        mapping.pop(task_name, None)
        _save_mapping(mapping)
        logger.info(f"[Feishu] Task cancelled: {task_name} ({event_id})")

    return result