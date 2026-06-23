"""Home Assistant MCP Server — 5 tools for smart home control."""

import json
import os
import tempfile
import threading
import time
import random
import string

# --- 配置文件 ---

CONFIG_PATH = os.path.expanduser("~/.niu/ha-config.json")
_config_lock = threading.Lock()
_config_event = threading.Event()


def _read_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(config: dict) -> None:
    dir_path = os.path.dirname(CONFIG_PATH) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, CONFIG_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    _config_event.set()


def _atomic_update(update_fn):
    with _config_lock:
        config = _read_config()
        result = update_fn(config)
        _write_config(config)
        return result


def _generate_trigger_id(existing_ids: set) -> str:
    while True:
        ts = int(time.time())
        rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=4))
        tid = f"ha_trig_{ts}_{rand}"
        if tid not in existing_ids:
            return tid


# --- Domain 映射 ---

DOMAIN_MAP = {
    "light": {"type": "灯", "actions": ["turn_on", "turn_off", "toggle", "set_brightness"]},
    "climate": {"type": "空调/温控", "actions": ["turn_on", "turn_off", "set_temperature"]},
    "sensor": {"type": "传感器", "actions": []},
    "switch": {"type": "开关", "actions": ["turn_on", "turn_off", "toggle"]},
    "fan": {"type": "风扇", "actions": ["turn_on", "turn_off", "toggle"]},
    "cover": {"type": "窗帘", "actions": ["open", "close", "toggle"]},
    "lock": {"type": "门锁", "actions": ["lock", "unlock"]},
    "humidifier": {"type": "加湿器", "actions": ["turn_on", "turn_off"]},
    "vacuum": {"type": "扫地机", "actions": ["turn_on", "turn_off"]},
    "media_player": {"type": "媒体", "actions": ["turn_on", "turn_off", "toggle"]},
    "camera": {"type": "摄像头", "actions": []},
    "scene": {"type": "场景", "actions": ["activate"]},
    "script": {"type": "脚本", "actions": ["run"]},
    "automation": {"type": "自动化", "actions": ["trigger", "turn_on", "turn_off"]},
}

EXCLUDED_DOMAINS = {
    "input_boolean", "input_number", "input_select", "input_button",
    "sun", "zone", "person", "update", "weather",
}

ACTION_SERVICE_MAP = {
    "turn_on": lambda d: f"{d}/turn_on",
    "turn_off": lambda d: f"{d}/turn_off",
    "toggle": lambda d: f"{d}/toggle",
    "activate": lambda d: "scene/turn_on",
    "run": lambda d: "script/turn_on",
    "trigger": lambda d: "automation/trigger",
    "set_brightness": lambda d: "light/turn_on",
    "set_temperature": lambda d: "climate/set_temperature",
    "open": lambda d: "cover/open_cover",
    "close": lambda d: "cover/close_cover",
    "lock": lambda d: "lock/lock",
    "unlock": lambda d: "lock/unlock",
}


# --- TOOL_SCHEMAS ---

TOOL_SCHEMAS = {}


def get_tool_schemas():
    return list(TOOL_SCHEMAS.values())
