"""飞书日历 API 客户端 — lark-oapi SDK 封装"""

import json
from pathlib import Path
from datetime import datetime, timezone

import lark_oapi as lark
from lark_oapi.api.calendar.v4.model import (
    CreateCalendarEventRequest,
    DeleteCalendarEventRequest,
    PatchCalendarEventRequest,
    CalendarEvent,
    TimeInfo,
)
from loguru import logger


# ============== Client Singleton ==============

_client: lark.Client | None = None


def get_feishu_client() -> lark.Client:
    """获取全局飞书 Client 单例"""
    global _client
    if _client is not None:
        return _client

    prefs_path = Path.home() / ".niu" / "preferences.json"
    if not prefs_path.exists():
        raise RuntimeError("preferences.json not found")

    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    feishu = prefs.get("feishu", {})
    app_id = feishu.get("app_id", "").strip()
    app_secret = feishu.get("app_secret", "").strip()

    if not app_id or not app_secret:
        raise RuntimeError("feishu app_id/app_secret not configured")

    _client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .log_level(lark.LogLevel.DEBUG) \
        .build()
    return _client


def feishu_sync_enabled() -> bool:
    """检查飞书日历同步是否启用"""
    prefs_path = Path.home() / ".niu" / "preferences.json"
    if not prefs_path.exists():
        return False
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        return prefs.get("feishu", {}).get("enabled", False)
    except Exception:
        return False


# ============== Helpers ==============

CALENDAR_ID = "primary"


def _iso_to_timestamp(iso_str: str) -> str:
    """ISO 8601 → 飞书时间戳字符串（秒级，UTC）

    无时区后缀的字符串视为 UTC。
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp()))


def _make_time_info(iso_str: str) -> TimeInfo:
    """ISO 8601 → TimeInfo 对象"""
    return TimeInfo.builder().timestamp(_iso_to_timestamp(iso_str)).build()


# ============== Calendar API ==============

def feishu_calendar_create(
    *, summary: str, start_time: str, end_time: str,
    recurrence: str | None = None, description: str = "",
) -> dict:
    """创建飞书日历事件

    Returns dict with ``event_id`` on success, or ``error`` on failure.
    """
    try:
        client = get_feishu_client()

        event_builder = CalendarEvent.builder() \
            .summary(summary) \
            .start_time(_make_time_info(start_time)) \
            .end_time(_make_time_info(end_time))

        if recurrence:
            event_builder = event_builder.recurrence(recurrence)
        if description:
            event_builder = event_builder.description(description)

        req = CreateCalendarEventRequest.builder() \
            .calendar_id(CALENDAR_ID) \
            .user_id_type("user_id") \
            .request_body(event_builder.build()) \
            .build()

        resp = client.calendar.v4.calendar_event.create(req)

        if not resp.success():
            logger.error(f"[Feishu] Create event failed: code={resp.code}, msg={resp.msg}")
            return {"error": f"创建失败: {resp.msg}"}

        event_id = resp.data.event.event_id
        logger.info(f"[Feishu] Event created: {event_id}")
        return {"event_id": event_id}

    except Exception as e:
        logger.error(f"[Feishu] Create event error: {e}")
        return {"error": str(e)}


def feishu_calendar_cancel(*, event_id: str) -> dict:
    """取消飞书日历事件

    Returns dict with ``success: True`` on success, or ``error`` on failure.
    """
    try:
        client = get_feishu_client()

        req = DeleteCalendarEventRequest.builder() \
            .calendar_id(CALENDAR_ID) \
            .event_id(event_id) \
            .user_id_type("user_id") \
            .build()

        resp = client.calendar.v4.calendar_event.delete(req)

        if not resp.success():
            logger.error(f"[Feishu] Cancel event failed: code={resp.code}, msg={resp.msg}")
            return {"error": f"取消失败: {resp.msg}"}

        logger.info(f"[Feishu] Event cancelled: {event_id}")
        return {"success": True}

    except Exception as e:
        logger.error(f"[Feishu] Cancel event error: {e}")
        return {"error": str(e)}


def feishu_calendar_update(
    *, event_id: str, summary: str | None = None,
    start_time: str | None = None, end_time: str | None = None,
    recurrence: str | None = None, description: str | None = None,
) -> dict:
    """更新飞书日历事件

    Returns dict with ``event_id`` on success, or ``error`` on failure.
    """
    try:
        client = get_feishu_client()

        event_builder = CalendarEvent.builder()
        if summary is not None:
            event_builder = event_builder.summary(summary)
        if start_time is not None:
            event_builder = event_builder.start_time(_make_time_info(start_time))
        if end_time is not None:
            event_builder = event_builder.end_time(_make_time_info(end_time))
        if recurrence is not None:
            event_builder = event_builder.recurrence(recurrence)
        if description is not None:
            event_builder = event_builder.description(description)

        req = PatchCalendarEventRequest.builder() \
            .calendar_id(CALENDAR_ID) \
            .event_id(event_id) \
            .user_id_type("user_id") \
            .request_body(event_builder.build()) \
            .build()

        resp = client.calendar.v4.calendar_event.patch(req)

        if not resp.success():
            logger.error(f"[Feishu] Update event failed: code={resp.code}, msg={resp.msg}")
            return {"error": f"更新失败: {resp.msg}"}

        updated_id = resp.data.event.event_id
        logger.info(f"[Feishu] Event updated: {updated_id}")
        return {"event_id": updated_id}

    except Exception as e:
        logger.error(f"[Feishu] Update event error: {e}")
        return {"error": str(e)}
