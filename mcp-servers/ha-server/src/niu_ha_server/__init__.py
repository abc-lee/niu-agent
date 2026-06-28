"""Home Assistant MCP Server — 5 tools for smart home control."""

import json
import os
import tempfile
import threading
import time
import random
import string
import uuid
import re

import asyncio as _asyncio

import requests as _requests

# --- 配置文件 ---

CONFIG_PATH = os.path.expanduser("~/.niu/ha-config.json")
SERVICES_CACHE_PATH = os.path.expanduser("~/.niu/ha-services.json")
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


def _read_services_cache() -> dict:
    if not os.path.exists(SERVICES_CACHE_PATH):
        return {}
    try:
        with open(SERVICES_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_services_cache(services: dict) -> None:
    dir_path = os.path.dirname(SERVICES_CACHE_PATH) or "."
    os.makedirs(dir_path, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(services, f, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        os.rename(tmp_path, SERVICES_CACHE_PATH)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _fetch_and_cache_services(ha_url: str, headers: dict) -> dict:
    """从 HA API 获取服务列表并缓存。返回 {domain: {service_name: {fields: {...}}}} 格式。
    仅在获取成功时写入缓存文件，失败时不写入（避免空文件覆盖旧缓存）。"""
    try:
        resp = _requests.get(f"{ha_url}/api/services", headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"[HA] 获取服务列表失败: HTTP {resp.status_code}")
            return {}
        raw = resp.json()
        # raw 格式: [{domain: "vacuum", services: {svc_name: {...}}}, ...]
        cache = {}
        for item in raw:
            domain = item.get("domain", "")
            services = item.get("services", {})
            if not domain or not services:
                continue
            cache[domain] = {}
            for svc_name, svc_info in services.items():
                fields = {}
                for fname, finfo in (svc_info.get("fields") or {}).items():
                    # 跳过 entity_id 字段（已作为顶层参数传递）
                    if fname == "entity_id":
                        continue
                    field_def = {"required": finfo.get("required", False)}
                    if finfo.get("description"):
                        field_def["description"] = finfo["description"]
                    if finfo.get("example") is not None:
                        field_def["example"] = finfo["example"]
                    # 提取 selector 中的选项（如 fan_speed 的可选值）
                    selector = finfo.get("selector", {})
                    if selector:
                        for sel_type, sel_data in selector.items():
                            if sel_type == "select" and "options" in sel_data:
                                opts = sel_data["options"]
                                if isinstance(opts, list):
                                    field_def["options"] = [
                                        o if isinstance(o, str) else (o.get("value", str(o)) if isinstance(o, dict) else str(o))
                                        for o in opts if o is not None
                                    ]
                            elif sel_type == "number":
                                if "min" in sel_data:
                                    field_def["min"] = sel_data["min"]
                                if "max" in sel_data:
                                    field_def["max"] = sel_data["max"]
                    fields[fname] = field_def
                cache[domain][svc_name] = {"fields": fields}
        if cache:
            _write_services_cache(cache)
        return cache
    except Exception as e:
        print(f"[HA] 获取服务列表异常: {e}")
        return {}


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


def _register_name(domain: str, name: str, slug: str) -> None:
    """在 HA config 中注册 name -> slug 映射，用于中文名称查找。"""
    def _update(config):
        key = f"{domain}_name_map"
        if key not in config:
            config[key] = {}
        config[key][name] = slug
        return config
    _atomic_update(_update)


def _unregister_name(domain: str, name: str) -> None:
    """从 HA config 中删除 name -> slug 映射。"""
    def _update(config):
        key = f"{domain}_name_map"
        if key in config and name in config[key]:
            del config[key][name]
        return config
    _atomic_update(_update)


def _lookup_slug(domain: str, name: str) -> str:
    """从 HA config 中查找 name 对应的 slug。"""
    config = _read_config()
    key = f"{domain}_name_map"
    return config.get(key, {}).get(name, "")


def _make_slug(name: str) -> str:
    """从名称生成合法的 HA entity_id slug。中文名称生成 UUID slug。"""
    try:
        name.encode('ascii')
    except UnicodeEncodeError:
        return uuid.uuid4().hex
    slug = re.sub(r'[^a-z0-9_]', '_', name.lower()).strip('_')
    if not slug:
        slug = uuid.uuid4().hex
    return slug


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

ATTR_WHITELIST = {
    "climate": [
        "current_temperature", "temperature", "target_temp_temp", "target_temp_high",
        "target_temp_low", "indoor_temperature", "indoor_humidity",
        "hvac_action", "hvac_mode", "preset_mode",
        "hvac_modes", "preset_modes", "fan_modes", "swing_modes",
        "swing_horizontal_modes",
    ],
    "sensor": [
        "unit_of_measurement", "device_class", "state_class",
    ],
    "light": [
        "brightness", "color_mode", "supported_color_modes",
    ],
    "switch": [],
    "fan": [
        "percentage", "percentage_step", "preset_modes",
        "speed_list", "direction",
    ],
    "cover": [
        "current_position", "current_tilt_position",
        "supported_features",
    ],
    "lock": [],
    "humidifier": [
        "humidity", "target_humidity", "mode", "available_modes",
    ],
    "vacuum": [
        "fan_speed", "fan_speed_list", "rooms",
        "supported_features",
    ],
    "media_player": [
        "source", "source_list", "media_title", "volume_level",
        "supported_features",
    ],
    "camera": [],
    "scene": [],
    "script": [],
    "automation": [
        "last_triggered",
    ],
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

# 向后兼容：旧 action 参数到 service 的映射
ACTION_COMPAT_MAP = {
    "turn_on": "turn_on",
    "turn_off": "turn_off",
    "toggle": "toggle",
    "set_brightness": "turn_on",
    "set_temperature": "set_temperature",
    "open": "open_cover",
    "close": "close_cover",
    "lock": "lock",
    "unlock": "unlock",
    "activate": "turn_on",
    "run": "turn_on",
    "trigger": "trigger",
}

# vacuum 域的 action 特殊映射（vacuum 没有 turn_on/turn_off）
VACUUM_ACTION_MAP = {
    "turn_on": "start",
    "turn_off": "return_to_base",
}


# --- HA 连接辅助 ---

def _get_ha_client(config: dict):
    url = config.get("ha_url", "").rstrip("/")
    token = config.get("ha_token", "")
    if not url or not token:
        return None, None, "未配置 Home Assistant"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    return url, headers, None


def _find_entity_by_name(states, domain, name):
    """在 states 列表中按 friendly_name 匹配 entity_id"""
    prefix = f"{domain}."
    candidates = [s for s in states if s.get("entity_id", "").startswith(prefix)]
    # 精确匹配 friendly_name 或 entity_id
    for s in candidates:
        if s.get("attributes", {}).get("friendly_name", "") == name:
            return s["entity_id"]
        if s["entity_id"] == f"{domain}.{name}":
            return s["entity_id"]
    # 模糊匹配
    name_lower = name.lower()
    for s in candidates:
        fn = s.get("attributes", {}).get("friendly_name", "").lower()
        if name_lower in fn or fn in name_lower:
            return s["entity_id"]
    return None


def _fetch_entity_registry(ha_url, headers):
    """通过 WebSocket 一次性获取 entity_registry"""
    token = headers.get("Authorization", "").replace("Bearer ", "")
    commands = [{"type": "config/entity_registry/list"}]
    results = _ws_batch_call(ha_url, token, commands)
    if not results or not results[0]:
        return []
    return results[0]


def _resolve_config_key(ha_url, headers, domain, entity_id, entity_registry=None):
    """通过 entity_id 查找 config_key（entity_registry 的 unique_id）。
    entity_registry: 可选的预查询结果，避免重复 WebSocket 调用。"""
    if domain == "script":
        return entity_id.split(".", 1)[1]
    if entity_registry is None:
        entity_registry = _fetch_entity_registry(ha_url, headers)
    for entry in entity_registry:
        if entry.get("entity_id") == entity_id:
            return entry.get("unique_id")
    return None


def _fetch_domain_states(ha_url, headers, domain):
    """GET /api/states 过滤指定 domain"""
    try:
        resp = _requests.get(f"{ha_url}/api/states", headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        prefix = f"{domain}."
        return [s for s in resp.json() if s.get("entity_id", "").startswith(prefix)]
    except Exception:
        return []


def _verify_entity_exists(ha_url, headers, domain, config_key, timeout=3):
    """创建后验证 entity 已注册。通过 entity_registry 的 unique_id 查找实际 entity_id。
    自动化/场景的 entity_id 由 HA 从 alias/name slugify 生成，不是 config_key 本身。
    先轮询轻量 REST states API 等待 entity 出现，再用一次 entity_registry 确认 unique_id。"""
    import time
    deadline = time.time() + timeout
    # 记录创建前已有的 entity
    pre_existing = set()
    try:
        resp = _requests.get(f"{ha_url}/api/states", headers=headers, timeout=10)
        if resp.status_code == 200:
            for s in resp.json():
                eid = s.get("entity_id", "")
                if eid.startswith(f"{domain}."):
                    pre_existing.add(eid)
    except Exception:
        pass
    # 阶段 1: 轮询 REST states，等待新 entity 出现
    while time.time() < deadline:
        try:
            resp = _requests.get(f"{ha_url}/api/states", headers=headers, timeout=10)
            if resp.status_code == 200:
                current = {s.get("entity_id") for s in resp.json() if s.get("entity_id", "").startswith(f"{domain}.")}
                if current - pre_existing:
                    break
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)
    # 阶段 2: 用一次 WebSocket 查询 entity_registry，通过 unique_id 确认
    entity_registry = _fetch_entity_registry(ha_url, headers)
    for entry in entity_registry:
        if entry.get("unique_id") == config_key and entry.get("entity_id", "").startswith(f"{domain}."):
            return entry["entity_id"]
    return None


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
    """WebSocket 单命令调用。"""
    import websockets

    async def _run():
        ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        async with websockets.connect(ws_url, max_size=5_000_000) as ws:
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_required":
                return {"error": f"Unexpected: {msg}"}
            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_ok":
                return {"error": "HA 认证失败"}
            command = dict(command)
            if "id" not in command:
                command["id"] = 1
            await ws.send(json.dumps(command))
            while True:
                result = json.loads(await _asyncio.wait_for(ws.recv(), timeout=timeout))
                if result.get("id") == command["id"]:
                    return result
                if result.get("type") == "event":
                    continue

    try:
        _asyncio.get_running_loop()
        # 在运行中的事件循环内，用线程池隔离
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_asyncio.run, _run())
            return future.result(timeout=timeout)
    except RuntimeError:
        return _asyncio.run(_run())


def _ws_batch_call(ha_url: str, ha_token: str, commands: list, timeout: float = 20) -> tuple:
    """WebSocket 批量命令调用（单连接多命令）。"""
    import websockets

    async def _run():
        ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        async with websockets.connect(ws_url, max_size=5_000_000) as ws:
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_required":
                raise ValueError(f"Unexpected: {msg}")
            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_ok":
                raise ValueError("HA 认证失败")
            results = []
            for i, cmd in enumerate(commands, 1):
                cmd_copy = dict(cmd)
                cmd_copy["id"] = i
                await ws.send(json.dumps(cmd_copy))
                while True:
                    resp = json.loads(await _asyncio.wait_for(ws.recv(), timeout=timeout))
                    if resp.get("id") == i:
                        break
                    if resp.get("type") == "event":
                        continue
                if resp.get("type") == "result" and resp.get("success"):
                    results.append(resp.get("result"))
                else:
                    results.append(None)
            return tuple(results)

    try:
        _asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(_asyncio.run, _run())
            return future.result(timeout=timeout)
    except RuntimeError:
        return _asyncio.run(_run())


# --- supported_features 位掩码常量 ---
# 来源：HA homeassistant/components/{domain}/const.py 的 EntityFeature 枚举
# vacuum: VacuumEntityFeature
VACUUM_TURN_ON = 1
VACUUM_TURN_OFF = 2
VACUUM_PAUSE = 4
VACUUM_STOP = 8
VACUUM_RETURN = 16
VACUUM_FAN_SPEED = 32
VACUUM_BATTERY = 64
VACUUM_STATUS = 128
VACUUM_SEND_COMMAND = 256
VACUUM_LOCATE = 512
VACUUM_CLEAN_SPOT = 1024
VACUUM_MAP = 2048
VACUUM_STATE = 4096
VACUUM_START = 8192

# fan: FanEntityFeature
FAN_SET_SPEED = 1
FAN_DIRECTION = 2
FAN_OSCILLATE = 4
FAN_PRESET_MODE = 8

# cover: CoverEntityFeature
COVER_OPEN = 1
COVER_CLOSE = 2
COVER_SET_POSITION = 4
COVER_STOP = 8
COVER_OPEN_TILT = 16
COVER_CLOSE_TILT = 32
COVER_SET_TILT_POSITION = 64
COVER_STOP_TILT = 128

# media_player: MediaPlayerEntityFeature
MEDIA_VOLUME_SET = 4
MEDIA_VOLUME_MUTE = 8
MEDIA_VOLUME_STEP = 1024
MEDIA_SELECT_SOURCE = 2048


def _entity_supports_service(attrs: dict, domain: str, service: str) -> bool:
    """判断实体是否支持某个服务。基于 supported_features 位掩码和属性列表。"""
    sf = attrs.get("supported_features")

    if service in ("turn_on", "turn_off", "toggle"):
        # 有位掩码的 domain 需要检查；其他 domain 这些服务通用
        if domain == "vacuum":
            if service == "turn_on":
                return isinstance(sf, int) and bool(sf & VACUUM_START)
            if service == "turn_off":
                return isinstance(sf, int) and bool(sf & VACUUM_RETURN)
        return True

    if domain == "vacuum":
        vacuum_map = {
            "start": VACUUM_START, "pause": VACUUM_PAUSE, "stop": VACUUM_STOP,
            "return_to_base": VACUUM_RETURN, "set_fan_speed": VACUUM_FAN_SPEED,
            "clean_spot": VACUUM_CLEAN_SPOT, "locate": VACUUM_LOCATE,
            "clean_area": VACUUM_MAP, "send_command": VACUUM_SEND_COMMAND,
        }
        bit = vacuum_map.get(service)
        if bit is not None:
            return isinstance(sf, int) and bool(sf & bit)
        return True

    if domain == "light":
        return True

    if domain == "climate":
        if service == "set_hvac_mode":
            return bool(attrs.get("hvac_modes"))
        if service == "set_preset_mode":
            return bool(attrs.get("preset_modes"))
        if service == "set_fan_mode":
            return bool(attrs.get("fan_modes"))
        if service == "set_swing_mode":
            return bool(attrs.get("swing_modes"))
        if service == "set_swing_horizontal_mode":
            return bool(attrs.get("swing_horizontal_modes"))
        if service == "set_humidity":
            return "humidity" in attrs or "target_humidity" in attrs
        return True

    if domain == "fan":
        if service in ("set_percentage", "increase_speed", "decrease_speed"):
            return isinstance(sf, int) and bool(sf & FAN_SET_SPEED)
        if service == "set_preset_mode":
            return (isinstance(sf, int) and bool(sf & FAN_PRESET_MODE)) or bool(attrs.get("preset_modes"))
        if service == "oscillate":
            return isinstance(sf, int) and bool(sf & FAN_OSCILLATE)
        if service == "set_direction":
            return isinstance(sf, int) and bool(sf & FAN_DIRECTION)
        return True

    if domain == "cover":
        cover_map = {
            "open_cover": COVER_OPEN, "close_cover": COVER_CLOSE,
            "set_cover_position": COVER_SET_POSITION, "stop_cover": COVER_STOP,
            "open_cover_tilt": COVER_OPEN_TILT, "close_cover_tilt": COVER_CLOSE_TILT,
            "set_cover_tilt_position": COVER_SET_TILT_POSITION,
            "stop_cover_tilt": COVER_STOP_TILT,
            "toggle_cover_tilt": COVER_OPEN_TILT | COVER_CLOSE_TILT,
        }
        bit = cover_map.get(service)
        if bit is not None:
            return isinstance(sf, int) and bool(sf & bit)
        return True

    if domain == "humidifier":
        if service == "set_mode":
            return bool(attrs.get("available_modes"))
        if service == "set_humidity":
            return "target_humidity" in attrs or "humidity" in attrs
        return True

    if domain == "media_player":
        if service == "volume_set":
            return isinstance(sf, int) and bool(sf & MEDIA_VOLUME_SET)
        if service in ("volume_up", "volume_down"):
            return isinstance(sf, int) and bool(sf & MEDIA_VOLUME_STEP)
        if service == "select_source":
            return (isinstance(sf, int) and bool(sf & MEDIA_SELECT_SOURCE)) or bool(attrs.get("source_list"))
        return True

    return True


def _get_entity_actions(domain: str, attrs: dict, services_cache: dict) -> list:
    """根据服务缓存和实体属性计算可用 actions 列表。"""
    domain_services = services_cache.get(domain, {})
    if not domain_services:
        info = DOMAIN_MAP.get(domain, {})
        # 映射旧式 action 名为 HA 服务名，去重保持与动态路径一致
        mapped = []
        seen = set()
        for act in info.get("actions", []):
            if domain == "vacuum" and act in VACUUM_ACTION_MAP:
                name = VACUUM_ACTION_MAP[act]
            elif act in ACTION_COMPAT_MAP:
                name = ACTION_COMPAT_MAP[act]
            else:
                name = act
            if name not in seen:
                seen.add(name)
                mapped.append(name)
        return mapped

    actions = []
    for svc_name in sorted(domain_services.keys()):
        if _entity_supports_service(attrs, domain, svc_name):
            actions.append(svc_name)
    return actions


# --- ha_status ---

def ha_status(area: str = "", domain: str = "") -> dict:
    """查询智能家居设备、场景、自动化的当前状态。"""
    config = _read_config()
    services_cache = _read_services_cache()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"connected": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

    try:
        states_resp = _requests.get(f"{url}/api/states", headers=headers, timeout=30)
        if states_resp.status_code != 200:
            return {"connected": False, "error": f"获取状态失败: HTTP {states_resp.status_code}"}
        states = states_resp.json()

        token = config.get("ha_token", "")
        devices, areas, entities = _ws_batch_call(url, token, [
            {"type": "config/device_registry/list"},
            {"type": "config/area_registry/list"},
            {"type": "config/entity_registry/list"},
        ])
        devices = devices or []
        areas = areas or []
        entities = entities or []
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
        for ent in (entities or []):
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
            "type": info["type"] if info else ent_domain,
            "state": state,
            "actions": _get_entity_actions(ent_domain, attrs, services_cache),
        }
        # 添加有参数的服务定义（用实体属性覆盖 domain 级别选项）
        entity_services = {}
        domain_svcs = services_cache.get(ent_domain, {})
        for act in entry["actions"]:
            if act in domain_svcs:
                fields = domain_svcs[act].get("fields", {})
                if fields:
                    svc_def = {"fields": {k: dict(v) for k, v in fields.items()}}
                    for fname, finfo in svc_def["fields"].items():
                        if ent_domain == "vacuum" and fname == "fan_speed" and attrs.get("fan_speed_list"):
                            finfo["options"] = attrs["fan_speed_list"]
                        elif ent_domain == "climate" and fname == "hvac_mode" and attrs.get("hvac_modes"):
                            finfo["options"] = attrs["hvac_modes"]
                        elif ent_domain == "climate" and fname == "preset_mode" and attrs.get("preset_modes"):
                            finfo["options"] = attrs["preset_modes"]
                        elif ent_domain == "climate" and fname == "fan_mode" and attrs.get("fan_modes"):
                            finfo["options"] = attrs["fan_modes"]
                        elif ent_domain == "climate" and fname == "swing_mode" and attrs.get("swing_modes"):
                            finfo["options"] = attrs["swing_modes"]
                        elif ent_domain == "humidifier" and fname == "mode" and attrs.get("available_modes"):
                            finfo["options"] = attrs["available_modes"]
                        elif ent_domain == "fan" and fname == "preset_mode" and attrs.get("preset_modes"):
                            finfo["options"] = attrs["preset_modes"]
                        elif ent_domain == "media_player" and fname == "source" and attrs.get("source_list"):
                            finfo["options"] = attrs["source_list"]
                    entity_services[act] = svc_def
        if entity_services:
            entry["services"] = entity_services
        # Extract useful properties from attributes
        whitelist = ATTR_WHITELIST.get(ent_domain, [])
        if whitelist:
            props = {}
            for attr_name in whitelist:
                val = attrs.get(attr_name)
                if val is not None:
                    props[attr_name] = val
            if props:
                entry["properties"] = props

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

        # 缓存 HA 服务列表
        _fetch_and_cache_services(ha_url, headers)

        try:
            from niu_api.internal.ha_watcher import check_and_start
            check_and_start()
        except Exception:
            pass

        return result

    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"connected": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

    conn = _check_ha_connection(url, headers)
    if not conn["connected"]:
        return conn

    # 刷新服务缓存
    _fetch_and_cache_services(url, headers)

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


# --- ha_control ---

def ha_control(entity_id: str, action: str = "", service: str = "",
               value: float = None, service_data: dict = None, **kwargs) -> dict:
    """控制智能家居设备。支持两种调用方式：
    1. 新方式：service + service_data（直接调用 HA 服务）
    2. 旧方式：action + value（向后兼容）
    service 和 action 不应同时传入；若同时传入，service 优先。"""
    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"success": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

    # 验证 entity_id 格式
    if not entity_id or "." not in entity_id or "/" in entity_id or ".." in entity_id:
        return {"success": False, "error": f"无效的 entity_id: '{entity_id}'，格式应为 'domain.name'"}

    domain = entity_id.split(".")[0]
    _used_action_compat = False

    if not service and action:
        _used_action_compat = True
        if domain == "vacuum" and action in VACUUM_ACTION_MAP:
            service = VACUUM_ACTION_MAP[action]
        else:
            compat = ACTION_COMPAT_MAP.get(action)
            if compat:
                service = compat
            else:
                service = action

    if not service:
        return {"success": False, "error": "必须提供 service 或 action 参数"}

    # 支持 "domain.service" 格式，提取纯 service 名
    if "." in service:
        svc_prefix, svc_name = service.split(".", 1)
        if svc_prefix == domain:
            service = svc_name
        else:
            return {"success": False, "error": f"服务 '{service}' 的域 '{svc_prefix}' 与实体域 '{domain}' 不匹配，请使用 '{domain}.{svc_name}' 或纯服务名 '{svc_name}'"}

    # 验证 service 名无路径遍历字符
    if "/" in service or ".." in service:
        return {"success": False, "error": f"无效的 service 名称: '{service}'"}

    data = {"entity_id": entity_id}

    if service_data:
        # 过滤 entity_id 防止覆盖
        data.update({k: v for k, v in service_data.items() if k != "entity_id"})

    if value is not None and _used_action_compat:
        if action == "set_brightness":
            if value < 0 or value > 100:
                return {"success": False, "error": f"brightness 范围 0-100，当前值: {value}"}
            data["brightness_pct"] = value
        elif action == "set_temperature":
            data["temperature"] = value

    # 验证 service 是否在缓存中
    services_cache = _read_services_cache()
    domain_services = services_cache.get(domain, {})
    if not services_cache:
        # 缓存未初始化，无法验证但给出提示
        pass
    elif service not in domain_services:
        available = sorted(domain_services.keys())
        return {
            "success": False,
            "error": f"服务 '{service}' 不适用于 {domain} 设备，可用服务: {available}",
        }

    # 验证必填字段
    if domain_services and service in domain_services:
        fields = domain_services[service].get("fields", {})
        for fname, finfo in fields.items():
            if finfo.get("required") and fname not in data:
                return {
                    "success": False,
                    "error": f"服务 {domain}.{service} 缺少必填参数: {fname}",
                    "missing_fields": [f for f, i in fields.items() if i.get("required") and f not in data],
                }

    ha_service = f"{domain}/{service}"
    try:
        resp = _requests.post(
            f"{url}/api/services/{ha_service}",
            headers=headers,
            json=data,
            timeout=15,
        )
        if resp.status_code != 200:
            return {"success": False, "error": f"服务调用失败: HTTP {resp.status_code} - {resp.text[:200]}"}

        try:
            changed = resp.json()
        except Exception:
            changed = []
        if not isinstance(changed, list):
            changed = []
        target = None
        for item in changed:
            if isinstance(item, dict) and item.get("entity_id") == entity_id:
                target = item
                break

        if target:
            return {
                "success": True,
                "entity_id": entity_id,
                "state": target.get("state", ""),
                "attributes": target.get("attributes", {}),
            }

        state_resp = _requests.get(
            f"{url}/api/states/{entity_id}",
            headers=headers,
            timeout=10,
        )
        if state_resp.status_code == 200:
            target = state_resp.json()
            return {
                "success": True,
                "entity_id": entity_id,
                "state": target.get("state", ""),
                "attributes": target.get("attributes", {}),
            }

        return {"success": True, "entity_id": entity_id, "state": "unknown"}

    except Exception as e:
        return {"success": False, "error": f"控制失败: {e}"}


# --- ha_subscribe ---

def ha_subscribe(entity_id: str = "", condition: str = "", value: float = None,
                 from_state: str = "", to_state: str = "",
                 description: str = "", trigger_id: str = "",
                 operation: str = "", **kwargs) -> dict:
    """订阅智能家居设备状态变化通知。"""
    if operation == "list":
        config = _read_config()
        return {"triggers": config.get("triggers", [])}

    if operation == "unsubscribe":
        if not trigger_id:
            return {"success": False, "error": "取消订阅时 trigger_id 必填"}

        def _remove(config):
            triggers = config.get("triggers", [])
            config["triggers"] = [t for t in triggers if t["id"] != trigger_id]
            return config

        _atomic_update(_remove)
        try:
            from niu_api.internal.ha_watcher import check_and_start
            check_and_start()
        except Exception:
            pass
        return {"success": True, "trigger_id": trigger_id, "message": "已取消订阅"}

    if not entity_id or not condition:
        return {"success": False, "error": "新增订阅时 entity_id 和 condition 必填"}
    if "." not in entity_id or "/" in entity_id or ".." in entity_id:
        return {"success": False, "error": f"无效的 entity_id: '{entity_id}'，格式应为 'domain.name'"}
    if condition not in ("state_change", "above", "below"):
        return {"success": False, "error": f"无效的 condition: {condition}，可选: state_change, above, below"}
    if condition in ("above", "below") and value is None:
        return {"success": False, "error": f"condition 为 {condition} 时 value 必填"}
    config = _read_config()
    if not config.get("ha_url") or not config.get("ha_token"):
        return {"success": False, "error": "请先使用 ha_setup 配置 Home Assistant 连接"}

    if not description:
        if value is not None:
            description = f"{entity_id} {condition} {value}"
        else:
            description = f"{entity_id} {condition}"

    def _add(config):
        if "triggers" not in config:
            config["triggers"] = []
        existing_ids = {t["id"] for t in config["triggers"]}
        tid = trigger_id or _generate_trigger_id(existing_ids)
        trigger_entry = {
            "id": tid,
            "entity_id": entity_id,
            "condition": condition,
        }
        if condition in ("above", "below") and value is not None:
            trigger_entry["threshold"] = value
        if condition == "state_change":
            if from_state:
                trigger_entry["from_state"] = from_state
            if to_state:
                trigger_entry["to_state"] = to_state
        trigger_entry["description"] = description
        config["triggers"].append(trigger_entry)
        return {"trigger_id": tid}

    result = _atomic_update(_add)
    try:
        from niu_api.internal.ha_watcher import check_and_start
        check_and_start()
    except Exception:
        pass
    return {
        "success": True,
        "trigger_id": result.get("trigger_id", ""),
        "message": f"已订阅: {description}",
    }


# --- ha_integrate ---

def _parse_data_schema(data_schema: list) -> list:
    fields = []
    if not data_schema:
        return fields
    for item in data_schema:
        if not isinstance(item, dict):
            continue
        field = {
            "name": item.get("name", ""),
            "type": item.get("type", "string"),
            "required": item.get("required", False),
            "label": item.get("label", item.get("name", "")),
        }
        if "options" in item:
            field["options"] = item["options"]
        if "default" in item:
            field["default"] = item["default"]
        fields.append(field)
    return fields


def ha_integrate(handler: str = "", flow_id: str = "", data: dict = None,
                 operation: str = "", entry_id: str = "", **kwargs) -> dict:
    """管理 Home Assistant 集成（添加/删除设备品牌集成）。"""
    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"success": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

    if operation == "delete":
        if not entry_id:
            return {"success": False, "error": "删除集成时 entry_id 必填"}
        resp = _requests.delete(
            f"{url}/api/config/config_entries/entry/{entry_id}",
            headers=headers, timeout=15,
        )
        if resp.status_code in (200, 204):
            return {"success": True, "message": "集成已删除"}
        return {"success": False, "error": f"删除失败: HTTP {resp.status_code}"}

    if not handler:
        return {"success": False, "error": "发起配置流时 handler 必填"}

    if not flow_id:
        try:
            resp = _requests.post(
                f"{url}/api/config/config_entries/flow",
                headers=headers,
                json={"handler": handler, "show_advanced_options": False, "context": {"source": "user"}},
                timeout=15,
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"发起配置流失败: HTTP {resp.status_code}"}
            result = resp.json()
        except Exception as e:
            return {"success": False, "error": f"发起配置流失败: {e}"}
    else:
        if not data:
            return {"success": False, "error": "推进配置流时 data 必填"}
        try:
            resp = _requests.post(
                f"{url}/api/config/config_entries/flow/{flow_id}",
                headers=headers,
                json=dict(data),
                timeout=15,
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"推进配置流失败: HTTP {resp.status_code}"}
            result = resp.json()
        except Exception as e:
            return {"success": False, "error": f"推进配置流失败: {e}"}

    flow_type = result.get("type", "")
    if flow_type == "form":
        fields = _parse_data_schema(result.get("data_schema", []))
        return {
            "type": "form",
            "flow_id": result.get("flow_id", ""),
            "step_id": result.get("step_id", ""),
            "title": result.get("title", handler),
            "fields": fields,
            "description": result.get("description", ""),
        }
    elif flow_type == "create_entry":
        entry = result.get("result", {}) if isinstance(result.get("result"), dict) else {}
        return {
            "type": "create_entry",
            "title": result.get("title", handler),
            "entry_id": entry.get("entry_id", ""),
            "message": f"集成配置成功: {result.get('title', handler)}",
        }
    elif flow_type == "abort":
        return {
            "type": "abort",
            "reason": result.get("reason", "未知原因"),
            "description": result.get("description", ""),
        }
    else:
        return result


# --- ha_automation ---

def ha_automation(action: str, name: str = "", config: dict = None, confirm: bool = False, detail: bool = False, **kwargs) -> dict:
    """管理自动化"""
    cfg = _read_config()
    ha_url, headers, err = _get_ha_client(cfg)
    if err:
        return {"error": err}

    if action == "list":
        states = _fetch_domain_states(ha_url, headers, "automation")
        # detail=true 时一次性获取 entity_registry，避免循环中重复 WebSocket 连接
        entity_registry = None
        if detail:
            entity_registry = _fetch_entity_registry(ha_url, headers)
        automations = []
        for s in states:
            attrs = s.get("attributes", {})
            entry = {
                "name": attrs.get("friendly_name", s["entity_id"]),
                "entity_id": s["entity_id"],
                "state": s.get("state", "off"),
                "last_triggered": attrs.get("last_triggered"),
            }
            if detail:
                config_key = _resolve_config_key(ha_url, headers, "automation", s["entity_id"], entity_registry)
                if config_key:
                    try:
                        resp = _requests.get(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, timeout=10)
                        if resp.status_code == 200:
                            entry["config"] = resp.json()
                    except Exception:
                        pass
            automations.append(entry)
        return {"automations": automations}

    if action == "get":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化，请先 list 查看可用列表"}
        config_key = _resolve_config_key(ha_url, headers, "automation", entity_id)
        if not config_key:
            return {"error": f"无法解析自动化的配置 ID: {entity_id}"}
        try:
            resp = _requests.get(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code == 200:
                return {"name": name, "entity_id": entity_id, "config": resp.json()}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "create":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        config = {**config}
        config.pop("id", None)  # 移除用户可能传入的 id，由 HA 内部设置
        config_key = uuid.uuid4().hex
        config["id"] = config_key
        config["alias"] = name
        try:
            resp = _requests.post(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                # 重载自动化集成使新 entity 立即可用
                try:
                    _requests.post(f"{ha_url}/api/services/automation/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                # 验证 entity 已注册
                actual_entity_id = _verify_entity_exists(ha_url, headers, "automation", config_key)
                return {"success": True, "name": name, "entity_id": actual_entity_id or f"automation.{_make_slug(name)}", "config_key": config_key}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "update":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        config_key = _resolve_config_key(ha_url, headers, "automation", entity_id)
        if not config_key:
            return {"error": f"无法解析自动化的配置 ID: {entity_id}"}
        config = {**config}
        config.pop("id", None)  # 移除用户可能传入的 id
        config["id"] = config_key
        config["alias"] = name
        try:
            resp = _requests.post(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                try:
                    _requests.post(f"{ha_url}/api/services/automation/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                return {"success": True, "name": name, "entity_id": entity_id}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "delete":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        if not confirm:
            attrs = next((s.get("attributes", {}) for s in states if s["entity_id"] == entity_id), {})
            return {"preview": True, "name": name, "entity_id": entity_id, "state": next((s.get("state") for s in states if s["entity_id"] == entity_id), ""), "last_triggered": attrs.get("last_triggered"), "message": "确认删除？请再次调用并传 confirm=true"}
        config_key = _resolve_config_key(ha_url, headers, "automation", entity_id)
        if not config_key:
            return {"error": f"无法解析自动化的配置 ID: {entity_id}"}
        try:
            resp = _requests.delete(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                # 重载自动化集成使删除生效
                try:
                    _requests.post(f"{ha_url}/api/services/automation/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                return {"success": True, "deleted": name}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action in ("enable", "disable"):
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        service = "automation.turn_on" if action == "enable" else "automation.turn_off"
        try:
            resp = _requests.post(f"{ha_url}/api/services/{service.replace('.', '/', 1)}", headers=headers, json={"entity_id": entity_id}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "action": action + "d"}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "trigger":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        try:
            resp = _requests.post(f"{ha_url}/api/services/automation/trigger", headers=headers, json={"entity_id": entity_id}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "triggered": True}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"未知操作: {action}"}


# --- ha_scene ---

def ha_scene(action: str, name: str = "", config: dict = None, confirm: bool = False, detail: bool = False, entity_ids: list = None, **kwargs) -> dict:
    """管理场景。使用 REST config API 持久化场景配置。"""
    cfg = _read_config()
    ha_url, headers, err = _get_ha_client(cfg)
    if err:
        return {"error": err}

    if action == "list":
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_registry = None
        if detail:
            entity_registry = _fetch_entity_registry(ha_url, headers)
        scenes = []
        seen_entity_ids = set()
        for s in states:
            attrs = s.get("attributes", {})
            eid = s["entity_id"]
            seen_entity_ids.add(eid)
            entry = {"name": attrs.get("friendly_name", eid), "entity_id": eid, "state": s.get("state", "off")}
            if detail:
                config_key = _resolve_config_key(ha_url, headers, "scene", eid, entity_registry)
                if config_key:
                    try:
                        resp = _requests.get(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, timeout=10)
                        if resp.status_code == 200:
                            entry["config"] = resp.json()
                    except Exception:
                        pass
                eid_attr = attrs.get("entity_id")
                if eid_attr:
                    entry["entities"] = eid_attr if isinstance(eid_attr, list) else [eid_attr]
            scenes.append(entry)
        # 补充：通过 name_map 查找 config API 创建但不在 states 中的场景
        scene_name_map = cfg.get("scene_name_map", {})
        for map_name, map_slug in scene_name_map.items():
            eid = f"scene.{map_slug}"
            if eid not in seen_entity_ids:
                entry = {"name": map_name, "entity_id": eid, "state": "idle"}
                if detail:
                    try:
                        resp = _requests.get(f"{ha_url}/api/config/scene/config/{map_slug}", headers=headers, timeout=10)
                        if resp.status_code == 200:
                            entry["config"] = resp.json()
                    except Exception:
                        pass
                scenes.append(entry)
        return {"scenes": scenes}

    if action == "get":
        if not name:
            return {"error": "name 参数必填"}
        # 先尝试通过 states 查找
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        config_key = None
        if entity_id:
            entity_registry = _fetch_entity_registry(ha_url, headers)
            config_key = _resolve_config_key(ha_url, headers, "scene", entity_id, entity_registry)
        if not config_key:
            # 尝试通过 name_map 查找
            slug = _lookup_slug("scene", name)
            if slug:
                config_key = slug
                entity_id = entity_id or f"scene.{slug}"
        if not config_key:
            return {"error": f"未找到名为 '{name}' 的场景"}
        try:
            resp = _requests.get(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code == 200:
                return {"name": name, "entity_id": entity_id, "config": resp.json()}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "create":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        config = {**config}
        config.pop("id", None)
        config_key = uuid.uuid4().hex
        config["name"] = name
        config["id"] = config_key
        try:
            resp = _requests.post(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                try:
                    _requests.post(f"{ha_url}/api/services/scene/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                _register_name("scene", name, config_key)
                return {"success": True, "name": name, "entity_id": f"scene.{config_key}", "config_key": config_key}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "update":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        # 查找 config_key
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        config_key = None
        if entity_id:
            entity_registry = _fetch_entity_registry(ha_url, headers)
            config_key = _resolve_config_key(ha_url, headers, "scene", entity_id, entity_registry)
        if not config_key:
            config_key = _lookup_slug("scene", name)
            if config_key:
                entity_id = entity_id or f"scene.{config_key}"
        if not config_key:
            return {"error": f"未找到名为 '{name}' 的场景"}
        config = {**config}
        config.pop("id", None)
        config["name"] = name
        config["id"] = config_key
        try:
            resp = _requests.post(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                try:
                    _requests.post(f"{ha_url}/api/services/scene/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                return {"success": True, "name": name, "entity_id": entity_id}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "delete":
        if not name:
            return {"error": "name 参数必填"}
        # 查找 config_key
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        config_key = None
        if entity_id:
            entity_registry = _fetch_entity_registry(ha_url, headers)
            config_key = _resolve_config_key(ha_url, headers, "scene", entity_id, entity_registry)
        if not config_key:
            config_key = _lookup_slug("scene", name)
            if config_key:
                entity_id = entity_id or f"scene.{config_key}"
        if not config_key:
            return {"error": f"未找到名为 '{name}' 的场景"}
        if not confirm:
            return {"preview": True, "name": name, "entity_id": entity_id or f"scene.{config_key}", "message": "确认删除？请再次调用并传 confirm=true"}
        try:
            resp = _requests.delete(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                try:
                    _requests.post(f"{ha_url}/api/services/scene/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                _unregister_name("scene", name)
                return {"success": True, "deleted": name}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "activate":
        if not name:
            return {"error": "name 参数必填"}
        # 查找 entity_id
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        if not entity_id:
            # 尝试通过 name_map
            config_key = _lookup_slug("scene", name)
            if config_key:
                entity_id = f"scene.{config_key}"
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的场景"}
        try:
            resp = _requests.post(f"{ha_url}/api/services/scene/turn_on", headers=headers, json={"entity_id": entity_id}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "activated": True}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "snapshot":
        """从当前设备状态创建场景快照并持久化。"""
        if not name or not entity_ids:
            return {"error": "name 和 entity_ids 参数必填"}
        # Validate entity_ids format
        for eid in entity_ids:
            if "." not in eid or "/" in eid or ".." in eid:
                return {"error": f"无效的 entity_id: '{eid}'，格式应为 'domain.name'"}
        entities_config = {}
        for eid in entity_ids:
            try:
                resp = _requests.get(f"{ha_url}/api/states/{eid}", headers=headers, timeout=10)
                if resp.status_code == 200:
                    state_data = resp.json()
                    entities_config[eid] = {"state": state_data["state"]}
                    attrs = state_data.get("attributes", {})
                    for key in ("brightness", "color_temp_kelvin", "target_temperature", "hvac_mode", "percentage", "current_cover_position", "target_humidity", "preset_mode", "fan_mode"):
                        if key in attrs:
                            entities_config[eid][key] = attrs[key]
            except Exception:
                pass
        if not entities_config:
            return {"error": "无法读取任何设备状态，请检查 entity_ids"}
        config_key = uuid.uuid4().hex
        scene_config = {"name": name, "id": config_key, "entities": entities_config}
        try:
            resp = _requests.post(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, json=scene_config, timeout=10)
            if resp.status_code in (200, 201):
                try:
                    _requests.post(f"{ha_url}/api/services/scene/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                _register_name("scene", name, config_key)
                return {"success": True, "name": name, "entity_id": f"scene.{config_key}", "entities": list(entities_config.keys())}
            return {"error": f"快照持久化失败: HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"未知操作: {action}"}


# --- ha_script ---

def _find_script_by_name(ha_url, headers, name):
    """在脚本 states 或 config API 中按名称查找脚本。
    返回 (entity_id, slug) 或 (None, None)。
    HA 2026+ 通过 config API 创建的脚本不在 states 中出现，
    但仍可通过 config API 按 slug 读取。"""

    # 方式 1: 在 states 中查找（YAML/UI 创建的脚本可能在 states 中）
    states = _fetch_domain_states(ha_url, headers, "script")
    for s in states:
        if s.get("attributes", {}).get("friendly_name", "") == name:
            slug = s["entity_id"].split(".", 1)[1]
            return s["entity_id"], slug
        if s["entity_id"] == f"script.{name}":
            return s["entity_id"], name
    # 模糊匹配
    name_lower = name.lower()
    for s in states:
        fn = s.get("attributes", {}).get("friendly_name", "").lower()
        if name_lower in fn or fn in name_lower:
            slug = s["entity_id"].split(".", 1)[1]
            return s["entity_id"], slug

    # 方式 2: 检查本地 name_map
    mapped_slug = _lookup_slug("script", name)
    if mapped_slug:
        # 验证脚本仍存在
        try:
            resp = _requests.get(f"{ha_url}/api/config/script/config/{mapped_slug}", headers=headers, timeout=10)
            if resp.status_code == 200:
                return f"script.{mapped_slug}", mapped_slug
        except Exception:
            pass
        # 映射失效，清除
        _unregister_name("script", name)

    # 方式 3: 按 slug 推算并在 config API 中验证
    slug = re.sub(r'[^a-z0-9_]', '_', name.lower()).strip('_')
    if slug:
        try:
            resp = _requests.get(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
            if resp.status_code == 200:
                config_data = resp.json()
                # 检查 alias 是否匹配
                alias = config_data.get("alias", "")
                if alias == name or name_lower in alias.lower():
                    return f"script.{slug}", slug
        except Exception:
            pass

    return None, None


def ha_script(action: str, name: str = "", config: dict = None, confirm: bool = False, detail: bool = False, **kwargs) -> dict:
    """管理脚本"""
    cfg = _read_config()
    ha_url, headers, err = _get_ha_client(cfg)
    if err:
        return {"error": err}

    if action == "list":
        # 优先从 states 获取（YAML/UI 创建的脚本）
        states = _fetch_domain_states(ha_url, headers, "script")
        scripts = []
        seen_entity_ids = set()
        for s in states:
            attrs = s.get("attributes", {})
            eid = s["entity_id"]
            seen_entity_ids.add(eid)
            entry = {"name": attrs.get("friendly_name", eid), "entity_id": eid, "state": s.get("state", "off")}
            if detail:
                slug = eid.split(".", 1)[1]
                try:
                    resp = _requests.get(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
                    if resp.status_code == 200:
                        entry["config"] = resp.json()
                except Exception:
                    pass
            scripts.append(entry)
        # 补充：通过 name_map 查找 config API 创建但不在 states 中的脚本
        script_name_map = cfg.get("script_name_map", {})
        for map_name, map_slug in script_name_map.items():
            eid = f"script.{map_slug}"
            if eid not in seen_entity_ids:
                entry = {"name": map_name, "entity_id": eid, "state": "idle"}
                if detail:
                    try:
                        resp = _requests.get(f"{ha_url}/api/config/script/config/{map_slug}", headers=headers, timeout=10)
                        if resp.status_code == 200:
                            entry["config"] = resp.json()
                    except Exception:
                        pass
                scripts.append(entry)
        return {"scripts": scripts}

    if action == "get":
        if not name:
            return {"error": "name 参数必填"}
        entity_id, slug = _find_script_by_name(ha_url, headers, name)
        if not slug:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        try:
            resp = _requests.get(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
            if resp.status_code == 200:
                result = {"name": name, "entity_id": entity_id or f"script.{slug}", "config": resp.json()}
                # 补充 state 信息
                if entity_id:
                    states = _fetch_domain_states(ha_url, headers, "script")
                    for s in states:
                        if s["entity_id"] == entity_id:
                            result["state"] = s.get("state", "off")
                            break
                return result
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "create":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        config = {**config}
        slug = _make_slug(name)
        config.pop("id", None)
        config["alias"] = name
        try:
            resp = _requests.post(f"{ha_url}/api/config/script/config/{slug}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                try:
                    _requests.post(f"{ha_url}/api/services/script/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                _register_name("script", name, slug)
                return {"success": True, "name": name, "entity_id": f"script.{slug}"}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "update":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        entity_id, slug = _find_script_by_name(ha_url, headers, name)
        if not slug:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        config = {**config}
        config.pop("id", None)
        config["alias"] = name
        # Script config API uses merge semantics — need delete+create for replace
        backup = None
        try:
            bk_resp = _requests.get(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
            if bk_resp.status_code == 200:
                backup = bk_resp.json()
        except Exception:
            pass
        try:
            _requests.delete(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
            resp = _requests.post(f"{ha_url}/api/config/script/config/{slug}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                try:
                    _requests.post(f"{ha_url}/api/services/script/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                return {"success": True, "name": name, "entity_id": entity_id or f"script.{slug}"}
            # create failed, try rollback
            if backup:
                try:
                    _requests.post(f"{ha_url}/api/config/script/config/{slug}", headers=headers, json=backup, timeout=10)
                except Exception:
                    pass
            return {"error": f"更新失败（已尝试回滚）: HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            if backup:
                try:
                    _requests.post(f"{ha_url}/api/config/script/config/{slug}", headers=headers, json=backup, timeout=10)
                except Exception:
                    pass
            return {"error": f"更新失败（已尝试回滚）: {e}"}

    if action == "delete":
        if not name:
            return {"error": "name 参数必填"}
        entity_id, slug = _find_script_by_name(ha_url, headers, name)
        if not slug:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        if not confirm:
            return {"preview": True, "name": name, "entity_id": entity_id or f"script.{slug}", "message": "确认删除？请再次调用并传 confirm=true"}
        try:
            resp = _requests.delete(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                try:
                    _requests.post(f"{ha_url}/api/services/script/reload", headers=headers, json={}, timeout=10)
                except Exception:
                    pass
                _unregister_name("script", name)
                return {"success": True, "deleted": name}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "run":
        if not name:
            return {"error": "name 参数必填"}
        entity_id, slug = _find_script_by_name(ha_url, headers, name)
        if not slug:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        # 脚本即使不在 states 中，仍可通过 script/turn_on 调用
        try:
            resp = _requests.post(f"{ha_url}/api/services/script/turn_on", headers=headers, json={"entity_id": entity_id or f"script.{slug}"}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "running": True}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"未知操作: {action}"}


def run_server():
    """MCP stdio server entry point (backup, same-process mode is primary)."""
    try:
        from mcp.server.stdio import stdio_server
        from mcp.server import Server
        server = Server("ha-server")

        @server.list_tools()
        async def list_tools():
            return get_tool_schemas()

        @server.call_tool()
        async def call_tool(name, arguments):
            fn = globals().get(name)
            if fn:
                result = fn(**arguments)
                from mcp.types import TextContent
                return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            return []

        import asyncio
        asyncio.run(stdio_server(server))
    except ImportError:
        print("MCP stdio mode requires 'mcp' package")


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
        "description": "查询智能家居设备、场景、自动化的当前状态。首次使用或需要了解可用设备时调用。返回按区域分类的设备列表，包含每个设备的可用操作及关键属性（温度、湿度等）。调用 ha_control 前建议先调用此工具确认设备状态和可用操作。可按 area 或 domain 过滤减少返回量。",
        "input_schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "按区域过滤，如 '书房'"},
                "domain": {"type": "string", "description": "按设备类型过滤，如 'light'、'climate'"},
            },
            "required": [],
        },
    },
    "ha_control": {
        "name": "ha_control",
        "description": "控制智能家居设备。两种调用方式：1) service + service_data（推荐，直接调用 HA 服务，如 service='start' 或 service='set_fan_speed' + service_data={'fan_speed': 'max'}）；2) action + value（兼容旧方式，如 action='turn_on'）。entity_id 从 ha_status 获取，可用 service 从 ha_status 返回的 actions 列表查看。有参数的服务其参数定义在 ha_status 返回的 services 字段中。service 和 action 至少提供一个。",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "实体 ID，如 vacuum.18603118098"},
                "action": {"type": "string", "description": "（兼容）动作名，如 turn_on/turn_off/toggle/set_brightness。与 service 互斥，优先使用 service"},
                "service": {"type": "string", "description": "HA 服务名，如 start/pause/stop/return_to_base/set_fan_speed。从 ha_status 的 actions 列表获取。与 action 互斥，优先使用此参数"},
                "value": {"type": "number", "description": "（兼容）亮度 0-100 或目标温度。仅在使用 action 模式时有效"},
                "service_data": {"type": "object", "description": "服务参数键值对，如 {'fan_speed': 'max'} 或 {'temperature': 26}。参数定义见 ha_status 的 services 字段"},
            },
            "required": ["entity_id"],
        },
    },
    "ha_subscribe": {
        "name": "ha_subscribe",
        "description": "订阅智能家居设备状态变化通知。支持 state_change（状态变化）、above（超过阈值）、below（低于阈值）三种条件。触发时通过 [智能家居] 前缀消息推送。operation='list' 查看当前订阅，operation='unsubscribe' 取消订阅。",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "监听的实体 ID（新增订阅时必填）"},
                "condition": {"type": "string", "enum": ["state_change", "above", "below"], "description": "条件类型（新增订阅时必填）"},
                "value": {"type": "number", "description": "above/below 时的阈值"},
                "from_state": {"type": "string", "description": "state_change 起始状态过滤"},
                "to_state": {"type": "string", "description": "state_change 目标状态过滤"},
                "description": {"type": "string", "description": "触发时的描述文本"},
                "trigger_id": {"type": "string", "description": "触发器标识，取消订阅时必填"},
                "operation": {"type": "string", "enum": ["unsubscribe", "list"], "description": "操作类型，不传表示新增"},
            },
            "required": [],
        },
    },
    "ha_integrate": {
        "name": "ha_integrate",
        "description": "管理 Home Assistant 集成（添加/删除设备品牌集成）。发起配置流时只需 handler 参数，返回表单字段后由 Agent 引导用户填写，再用 flow_id + data 推进。operation='delete' 删除已有集成。",
        "input_schema": {
            "type": "object",
            "properties": {
                "handler": {"type": "string", "description": "集成域名，如 xiaomi_miot"},
                "flow_id": {"type": "string", "description": "配置流 ID，推进步骤时必填"},
                "data": {"type": "object", "description": "表单数据键值对，推进步骤时必填"},
                "operation": {"type": "string", "enum": ["delete"], "description": "操作类型，delete 表示删除集成"},
                "entry_id": {"type": "string", "description": "集成条目 ID，删除时必填"},
            },
            "required": [],
        },
    },
    "ha_automation": {
        "name": "ha_automation",
        "description": "管理自动化：创建/查看/修改(update)/删除/启用/禁用/手动触发自动化。自动化是条件触发持续生效的规则（如'湿度>70%开除湿'、'日落开灯'）。立即执行一次用 ha_control，定时一次用 scheduler。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "enable", "disable", "trigger"],
                    "description": "操作类型：list=列出所有，get=查看配置，create=创建，update=更新，delete=删除，enable=启用，disable=禁用，trigger=手动触发",
                },
                "name": {
                    "type": "string",
                    "description": "自动化名称（get/create/update/delete/enable/disable/trigger 时必填）",
                },
                "config": {
                    "type": "object",
                    "description": "自动化配置 JSON。triggers: 触发条件列表，conditions: 执行条件列表，actions: 动作列表。trigger platform: state/numeric_state/time/time_pattern/sun/zone/event/template/mqtt/calendar/webhook/homeassistant。condition type: state/numeric_state/time/sun/zone/template/trigger/and/or/not。action type: 服务调用(用action键)/delay/wait_for_trigger/wait_template/choose/if/repeat/parallel/condition/variables/event/scene/stop。mode: single|restart|queued|parallel",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "删除确认（delete 操作第二次调用时传 true）",
                },
                "detail": {
                    "type": "boolean",
                    "description": "list 时是否返回完整配置（默认 false，只返回摘要）",
                },
            },
            "required": ["action"],
        },
    },
    "ha_scene": {
        "name": "ha_scene",
        "description": "管理场景：创建/查看/修改(update)/删除/激活/快照场景。场景是多设备瞬间切换到预设状态（如'阅读模式'、'晚安模式'）。有序列有延时用 ha_script，条件触发用 ha_automation。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "activate", "snapshot"],
                    "description": "操作类型：list=列出所有，get=查看配置，create=创建，update=更新，delete=删除，activate=激活场景，snapshot=从当前设备状态创建场景快照",
                },
                "name": {"type": "string", "description": "场景名称"},
                "config": {
                    "type": "object",
                    "description": "场景配置 JSON。entities: 设备状态快照，键为 entity_id，值为目标状态字典。支持 light(亮度/色温)、climate(温度/模式)、switch、lock、cover(位置)、fan(转速)、humidifier(湿度)",
                },
                "confirm": {"type": "boolean", "description": "删除确认"},
                "detail": {"type": "boolean", "description": "list 时是否返回完整配置"},
                "entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "snapshot 操作要快照的设备 entity_id 列表",
                },
            },
            "required": ["action"],
        },
    },
    "ha_script": {
        "name": "ha_script",
        "description": "管理脚本：创建/查看/修改(update)/删除/运行脚本。脚本是有序列、有延时的多步骤操作（如'先关灯等5秒再锁门'）。瞬间切换用 ha_scene，条件触发用 ha_automation。sequence 动作类型与自动化 actions 相同。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "run"],
                    "description": "操作类型：list=列出所有，get=查看配置，create=创建，update=更新，delete=删除，run=执行脚本",
                },
                "name": {"type": "string", "description": "脚本名称"},
                "config": {
                    "type": "object",
                    "description": "脚本配置 JSON。alias: 名称，mode: single|restart|queued|parallel，sequence: 步骤列表。步骤类型: 服务调用(action键)/delay/wait_for_trigger/choose/if/repeat/parallel/condition",
                },
                "confirm": {"type": "boolean", "description": "删除确认"},
                "detail": {"type": "boolean", "description": "list 时是否返回完整配置"},
            },
            "required": ["action"],
        },
    },
}


def get_tool_schemas():
    return list(TOOL_SCHEMAS.values())
