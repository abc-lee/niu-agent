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
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_required":
                raise ValueError(f"Unexpected first message: {msg}")
            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_ok":
                raise ValueError("HA 认证失败")
            await ws.send(json.dumps(command))
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
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


def _ws_batch_call(ha_url: str, ha_token: str, commands: list, timeout: float = 20) -> tuple:
    """WebSocket 短连接单次握手发多个命令，返回结果元组。"""
    import websockets

    ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"

    async def _run():
        async with websockets.connect(ws_url, max_size=5_000_000) as ws:
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_required":
                raise ValueError(f"Unexpected first message: {msg}")
            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
            if msg.get("type") != "auth_ok":
                raise ValueError(f"HA 认证失败")
            results = []
            for i, cmd in enumerate(commands, 1):
                cmd_copy = dict(cmd)
                cmd_copy["id"] = i
                await ws.send(json.dumps(cmd_copy))
                while True:
                    resp = json.loads(await _asyncio.wait_for(ws.recv(), timeout=10))
                    if resp.get("id") == i:
                        break
                if resp.get("type") == "result" and resp.get("success"):
                    results.append(resp.get("result"))
                else:
                    results.append(None)
            return tuple(results)

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
        return {"connected": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

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


# --- ha_control ---

def ha_control(entity_id: str, action: str, value: float = None, **kwargs) -> dict:
    """控制智能家居设备。"""
    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"success": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

    domain = entity_id.split(".")[0]
    info = DOMAIN_MAP.get(domain)
    if not info:
        return {"success": False, "error": f"未知的设备类型: {domain}"}

    if action not in info["actions"]:
        return {
            "success": False,
            "error": f"动作 '{action}' 不适用于 {info['type']} 设备，可用动作: {info['actions']}",
        }

    service = ACTION_SERVICE_MAP[action](domain)

    service_data = {"entity_id": entity_id}
    if action == "set_brightness" and value is not None:
        if value < 0 or value > 100:
            return {"success": False, "error": f"brightness 范围 0-100，当前值: {value}"}
        service_data["brightness"] = int(value * 2.55)
    elif action == "set_temperature" and value is not None:
        service_data["temperature"] = value

    try:
        resp = _requests.post(
            f"{url}/api/services/{service}",
            headers=headers,
            json=service_data,
            timeout=15,
        )
        if resp.status_code != 200:
            return {"success": False, "error": f"服务调用失败: HTTP {resp.status_code}"}

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
        return {"success": True, "trigger_id": trigger_id, "message": "已取消订阅"}

    if not entity_id or not condition:
        return {"success": False, "error": "新增订阅时 entity_id 和 condition 必填"}
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
                json={"handler": handler, "show_options": False},
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
    "ha_control": {
        "name": "ha_control",
        "description": "控制智能家居设备。需要 entity_id 和 action 参数。entity_id 从 ha_status 获取，action 必须在该设备允许的 actions 列表中。set_brightness 的 value 范围 0-100，set_temperature 的 value 为目标温度。",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "实体 ID，如 light.xxx"},
                "action": {"type": "string", "description": "动作名，如 turn_on/turn_off/toggle/set_brightness 等"},
                "value": {"type": "number", "description": "动作参数：亮度 0-100 或温度值"},
            },
            "required": ["entity_id", "action"],
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
}


def get_tool_schemas():
    return list(TOOL_SCHEMAS.values())
