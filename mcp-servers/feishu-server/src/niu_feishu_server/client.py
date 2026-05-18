"""飞书 API 客户端 -- lark-oapi SDK 封装"""

import json
from pathlib import Path
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


def get_feishu_client():
    """获取飞书 API 客户端（tenant_access_token 自动管理）"""
    import lark_oapi as lark

    config = _load_feishu_config()
    app_id = config.get("app_id", "")
    app_secret = config.get("app_secret", "")

    if not app_id or not app_secret:
        raise ValueError("Feishu app_id/app_secret not configured")

    client = lark.Client.builder() \
        .app_id(app_id) \
        .app_secret(app_secret) \
        .build()

    return client


def feishu_sync_enabled() -> bool:
    """检查飞书日历同步是否启用"""
    config = _load_feishu_config()
    return config.get("enabled", False) and config.get("sync", {}).get("calendar", False)


def feishu_calendar_create(
    *,
    summary: str,
    start_time: str,
    end_time: str,
    recurrence: str | None = None,
    description: str = "",
) -> dict:
    """创建飞书日历事件。

    Returns a dict with ``event_id`` on success, or ``error`` on failure.
    TODO: implement real lark-oapi call
    """
    return {"error": "not implemented"}


def feishu_calendar_cancel(event_id: str) -> dict:
    """取消飞书日历事件。

    Returns a dict with ``success: True`` on success, or ``error`` on failure.
    TODO: implement real lark-oapi call
    """
    return {"error": "not implemented"}


def feishu_calendar_update(
    *,
    event_id: str,
    summary: str,
    start_time: str,
    end_time: str,
    recurrence: str | None = None,
    description: str = "",
) -> dict:
    """更新飞书日历事件。

    Returns a dict with ``success: True`` on success, or ``error`` on failure.
    TODO: implement real lark-oapi call
    """
    return {"error": "not implemented"}
