"""飞书 API 客户端 -- lark-oapi SDK 封装

所有函数返回 dict（不返回 JSON 字符串），供 sync.py 和 __init__.py 统一使用。
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from loguru import logger


def _load_feishu_config() -> dict:
    """从 preferences.json 加载飞书配置"""
    prefs_path = Path.home() / ".niu" / "preferences.json"
    if not prefs_path.exists():
        return {}
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        return prefs.get("feishu", {})
    except Exception as e:
        logger.warning(f"Failed to load feishu config: {e}")
        return {}


def feishu_sync_enabled() -> bool:
    """检查飞书日历同步是否启用"""
    config = _load_feishu_config()
    return config.get("enabled", False) and config.get("sync", {}).get("calendar", False)


# ============== Client Singleton ==============

_client = None


def get_feishu_client():
    """获取飞书 API 客户端（单例，tenant_access_token 自动管理）"""
    global _client
    if _client is not None:
        return _client

    import lark_oapi as lark

    config = _load_feishu_config()
    app_id = config.get("app_id", "")
    app_secret = config.get("app_secret", "")

    if not app_id or not app_secret:
        raise ValueError("Feishu app_id/app_secret not configured")

    _client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .build()

    return _client


def _iso_to_timestamp(iso_str: str) -> int:
    """ISO 时间字符串 -> Unix 时间戳（秒）

    无时区信息的时间字符串统一假设为 UTC。
    """
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# ============== Calendar API ==============

def feishu_calendar_create(
    *,
    summary: str,
    start_time: str,
    end_time: str,
    recurrence: str | None = None,
    description: str = "",
) -> dict:
    """创建飞书日历事件。

    Returns dict with ``event_id`` on success, or ``error`` on failure.
    """
    try:
        from lark_oapi.api.calendar.v4 import CreateEventRequest, CreateEventRequestBody

        client = get_feishu_client()

        body = CreateEventRequestBody.builder() \
            .summary(summary) \
            .description(description) \
            .start_time({"timestamp": _iso_to_timestamp(start_time)}) \
            .end_time({"timestamp": _iso_to_timestamp(end_time)}) \
            .build()

        if recurrence:
            body.recurrence = [recurrence]

        request = CreateEventRequest.builder() \
            .calendar_id("primary") \
            .request_body(body) \
            .build()

        response = client.calendar.v4.event.create(request)

        if response.success():
            event_id = response.data.event.event_id
            logger.info(f"[Feishu] Calendar event created: {event_id}")
            return {"event_id": event_id}
        else:
            logger.error(f"[Feishu] Calendar create failed: {response.code} {response.msg}")
            return {"error": f"{response.code}: {response.msg}"}

    except Exception as e:
        logger.error(f"[Feishu] Calendar create error: {e}")
        return {"error": str(e)}


def feishu_calendar_cancel(event_id: str) -> dict:
    """取消飞书日历事件。

    Returns dict with ``success: True`` on success, or ``error`` on failure.
    """
    try:
        from lark_oapi.api.calendar.v4 import DeleteEventRequest

        client = get_feishu_client()

        request = DeleteEventRequest.builder() \
            .calendar_id("primary") \
            .event_id(event_id) \
            .build()

        response = client.calendar.v4.event.delete(request)

        if response.success():
            logger.info(f"[Feishu] Calendar event cancelled: {event_id}")
            return {"success": True}
        else:
            logger.error(f"[Feishu] Calendar cancel failed: {response.code} {response.msg}")
            return {"error": f"{response.code}: {response.msg}"}

    except Exception as e:
        logger.error(f"[Feishu] Calendar cancel error: {e}")
        return {"error": str(e)}


def feishu_calendar_update(
    *,
    event_id: str,
    summary: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    recurrence: str | None = None,
    description: str | None = None,
) -> dict:
    """更新飞书日历事件。

    Returns dict with ``success: True`` on success, or ``error`` on failure.
    """
    try:
        from lark_oapi.api.calendar.v4 import PatchEventRequest, PatchEventRequestBody

        client = get_feishu_client()

        body_builder = PatchEventRequestBody.builder()
        if summary is not None:
            body_builder = body_builder.summary(summary)
        if start_time is not None:
            body_builder = body_builder.start_time({"timestamp": _iso_to_timestamp(start_time)})
        if end_time is not None:
            body_builder = body_builder.end_time({"timestamp": _iso_to_timestamp(end_time)})
        if description is not None:
            body_builder = body_builder.description(description)

        body = body_builder.build()
        if recurrence is not None:
            body.recurrence = [recurrence]

        request = PatchEventRequest.builder() \
            .calendar_id("primary") \
            .event_id(event_id) \
            .request_body(body) \
            .build()

        response = client.calendar.v4.event.patch(request)

        if response.success():
            logger.info(f"[Feishu] Calendar event updated: {event_id}")
            return {"success": True}
        else:
            logger.error(f"[Feishu] Calendar update failed: {response.code} {response.msg}")
            return {"error": f"{response.code}: {response.msg}"}

    except Exception as e:
        logger.error(f"[Feishu] Calendar update error: {e}")
        return {"error": str(e)}