"""HAWatcher — Home Assistant WebSocket 守护线程，条件触发推送。"""
from niu_api.internal.ha_watcher.watcher import start_watcher, stop_watcher, check_and_start

__all__ = ["start_watcher", "stop_watcher", "check_and_start"]
