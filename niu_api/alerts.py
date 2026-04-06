"""提醒队列"""
import threading
from typing import List, Dict
from datetime import datetime

_pending_alerts: List[Dict] = []
_alerts_lock = threading.Lock()


def add_pending_alert(content: str):
    """添加待推送提醒"""
    with _alerts_lock:
        _pending_alerts.append({
            "content": content,
            "timestamp": datetime.now().isoformat()
        })


def get_and_clear_pending_alerts() -> List[Dict]:
    """获取并清空待推送提醒"""
    with _alerts_lock:
        alerts = _pending_alerts.copy()
        _pending_alerts.clear()
        return alerts
