"""Home Assistant MCP Server — 5 tools for smart home control."""

import json
import os
import tempfile
import threading
import time
import random
import string

import asyncio as _asyncio

import requests as _requests

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


# --- HA 连接辅助 ---

def _get_ha_client(config: dict):
    url = config.get("ha_url", "").rstrip("/")
    token = config.get("ha_token", "")
    if not url or not token:
        return None, None, "未配置 Home Assistant"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return url, headers, None


def _check_ha_connection(ha_url: str, headers: dict) -> dict:
    try:
        resp = _requests.get(f"{ha_url}/api/", headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return {"connected": True, "version": data.get("ha_version", "unknown")}
        return {"connected": False, "error": f"HA 返回状态码 {resp.status_code}"}
    except _requests.ConnectionError:
        return {"connected": False, "error": f"无法连接到 Home Assistant: {ha_url}"}
    except Exception as e:
        return {"connected": False, "error": f"连接异常: {str(e)}"}


def _ws_call(ha_url: str, ha_token: str, command: dict, timeout: float = 15) -> dict:
    """WebSocket 短连接：连接 → 认证 → 发送命令 → 接收结果 → 关闭。"""
    import websockets

    ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"

    async def _run():
        async with websockets.connect(ws_url, max_size=5_000_000) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise ValueError(f"Unexpected first message: {msg}")
            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise ValueError(f"HA 认证失败: {msg}")
            await ws.send(json.dumps(command))
            msg = json.loads(await ws.recv())
            if msg.get("type") == "result" and msg.get("success"):
                return msg.get("result")
            elif msg.get("type") == "result":
                raise ValueError(f"WS 命令失败: {msg.get('error')}")
            return msg

    try:
        loop = _asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_asyncio.run, _run())
                return future.result(timeout=timeout)
        return loop.run_until_complete(_run())
    except RuntimeError:
        return _asyncio.run(_run())


# --- ha_status ---

def ha_status(area: str = "", domain: str = "") -> dict:
    """查询智能家居设备、场景、自动化的当前状态。"""
    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"connected": False, "error": err}

    try:
        states_resp = _requests.get(f"{url}/api/states", headers=headers, timeout=30)
        if states_resp.status_code != 200:
            return {"connected": False, "error": f"获取状态失败: HTTP {states_resp.status_code}"}
        states = states_resp.json()

        token = config.get("ha_token", "")
        devices = _ws_call(url, token, {"id": 1, "type": "config/device_registry/list"}) or []
        areas = _ws_call(url, token, {"id": 2, "type": "config/area_registry/list"}) or []
    except Exception as e:
        return {"connected": False, "error": f"查询失败: {e}"}

    area_map = {a["area_id"]: a["name"] for a in areas}

    device_info = {}
    for dev in devices:
        dev_name = dev.get("name_by_user") or dev.get("name", "")
        dev_area = area_map.get(dev.get("area_id"), "")
        device_info[dev.get("id")] = {"name": dev_name, "area": dev_area}

    entity_device_map = {}
    try:
        token = config.get("ha_token", "")
        entities = _ws_call(url, token, {"id": 3, "type": "config/entity_registry/list"}) or []
        for ent in entities:
            did = ent.get("device_id")
            if did and did in device_info:
                entity_device_map[ent.get("entity_id", "")] = device_info[did]
    except Exception:
        pass

    result_devices = []
    result_scenes = []
    result_automations = []
    result_areas = [{"id": a["area_id"], "name": a["name"]} for a in areas]

    for entity in states:
        eid = entity.get("entity_id", "")
        ent_domain = eid.split(".")[0]

        if ent_domain in EXCLUDED_DOMAINS:
            continue
        if domain and ent_domain != domain:
            continue

        info = DOMAIN_MAP.get(ent_domain)
        if not info:
            continue

        attrs = entity.get("attributes", {})
        name = attrs.get("friendly_name", eid)
        state = entity.get("state", "")

        dev_info = entity_device_map.get(eid, {})
        area_name = dev_info.get("area", "")

        if area and area not in name and area not in area_name:
            continue

        entry = {
            "name": name,
            "area": area_name,
            "entity_id": eid,
            "type": info["type"],
            "state": state,
            "actions": info["actions"],
        }

        if ent_domain == "scene":
            result_scenes.append(entry)
        elif ent_domain == "automation":
            result_automations.append(entry)
        else:
            result_devices.append(entry)

    return {
        "connected": True,
        "areas": result_areas,
        "devices": result_devices,
        "scenes": result_scenes,
        "automations": result_automations,
    }


# --- ha_setup ---

def ha_setup(ha_url: str = "", ha_token: str = "") -> dict:
    """配置 Home Assistant 连接。"""
    if ha_url and ha_token:
        ha_url = ha_url.rstrip("/")
        headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}
        conn = _check_ha_connection(ha_url, headers)
        if not conn["connected"]:
            return conn

        def _setup(config):
            config["ha_url"] = ha_url
            config["ha_token"] = ha_token
            if "triggers" not in config:
                config["triggers"] = []
            return conn

        result = _atomic_update(_setup)
        result["ha_url"] = ha_url

        try:
            from niu_api.internal.ha_watcher import check_and_start
            check_and_start()
        except Exception:
            pass

        return result

    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"connected": False, "error": "未配置 Home Assistant"}

    conn = _check_ha_connection(url, headers)
    if not conn["connected"]:
        return conn

    conn["ha_url"] = url
    triggers = config.get("triggers", [])
    conn["triggers"] = [
        {
            "id": t["id"],
            "entity_id": t["entity_id"],
            "condition": t["condition"],
            **({"threshold": t["threshold"]} if "threshold" in t else {}),
            "description": t.get("description", ""),
        }
        for t in triggers
    ]
    return conn


# --- TOOL_SCHEMAS ---

TOOL_SCHEMAS = {
    "ha_setup": {
        "name": "ha_setup",
        "description": "配置 Home Assistant 连接。首次使用时传入 ha_url 和 ha_token。无参数时返回当前连接状态和订阅列表。ha_token 从 HA Web UI → 用户头像 → Security → Long-Lived Access Tokens 获取。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ha_url": {"type": "string", "description": "HA 地址，如 http://localhost:8123"},
                "ha_token": {"type": "string", "description": "Long-Lived Access Token"},
            },
            "required": [],
        },
    },
    "ha_status": {
        "name": "ha_status",
        "description": "查询智能家居设备、场景、自动化的当前状态。首次使用或需要了解可用设备时调用。返回按区域分类的设备列表，包含每个设备的可用操作。调用 ha_control 前建议先调用此工具确认设备状态和可用操作。可按 area 或 domain 过滤减少返回量。",
        "input_schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "按区域过滤，如 '书房'"},
                "domain": {"type": "string", "description": "按设备类型过滤，如 'light'、'climate'"},
            },
            "required": [],
        },
    },
}


def get_tool_schemas():
    return list(TOOL_SCHEMAS.values())
