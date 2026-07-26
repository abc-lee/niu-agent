# 智能家居集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 ha-server MCP Server（5 个工具）+ HAWatcher 守护线程 + 配置管理，让 Agent 能控制智能家居设备并接收条件触发推送。

**Architecture:** 同进程 MCP Server（ToolRegistry 模式），5 个工具封装 HA REST/WebSocket API。HAWatcher 守护线程（与 scheduler-server 同模式）通过 WebSocket subscribe_trigger 监听条件，触发时经 ChatQueue 推送。配置存储在 `~/.niu/ha-config.json`，原子写入 + 写入锁保护。

**Tech Stack:** Python 3.11+, requests（REST API）, websockets（WebSocket 长连接）, threading（守护线程 + 锁）, asyncio（WebSocket 事件循环）

---

## File Structure

| 文件 | 职责 |
|------|------|
| `mcp-servers/ha-server/src/niu_ha_server/__init__.py` | TOOL_SCHEMAS + 5 个工具函数 + HA REST 客户端 + 配置文件读写 + domain 映射 |
| `mcp-servers/ha-server/src/niu_ha_server/__main__.py` | MCP stdio 入口点（备用） |
| `mcp-servers/ha-server/pyproject.toml` | 包定义 |
| `niu_api/internal/ha_watcher/__init__.py` | 导出 start_watcher/stop_watcher/check_and_start |
| `niu_api/internal/ha_watcher/watcher.py` | HAWatcher 守护线程：WebSocket 长连接 + subscribe_trigger + ChatQueue 推送 |
| `config/mcp-servers.yaml` | 添加 ha-server 配置 |
| `agent/mcp_loader.py` | REQUIRED_SERVERS 添加 ha-server |

---

## Task 1: 项目骨架 + 配置文件读写 + Domain 映射

**Files:**
- Create: `mcp-servers/ha-server/pyproject.toml`
- Create: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`
- Create: `mcp-servers/ha-server/src/niu_ha_server/__main__.py`

- [ ] **Step 1: 创建目录结构**

```bash
mkdir -p mcp-servers/ha-server/src/niu_ha_server
```

- [ ] **Step 2: 创建 pyproject.toml**

```toml
[project]
name = "niu-ha-server"
version = "0.1.0"
description = "Home Assistant MCP Server for niu-agent"
requires-python = ">=3.11"
dependencies = [
    "requests",
    "websockets",
]
```

- [ ] **Step 3: 创建 __main__.py**

```python
"""MCP stdio entry point for ha-server (backup, same-process mode is primary)."""
from niu_ha_server import run_server

if __name__ == "__main__":
    run_server()
```

- [ ] **Step 4: 创建 __init__.py — 配置读写 + 常量 + 空的 TOOL_SCHEMAS**

```python
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
```

- [ ] **Step 5: 验证模块可导入**

```bash
cd mcp-servers/ha-server && PYTHONPATH=src python -c "from niu_ha_server import get_tool_schemas; print('OK:', get_tool_schemas())"
```

Expected: `OK: []`

- [ ] **Step 6: 提交**

```bash
git add mcp-servers/ha-server/
git commit -m "feat: add ha-server skeleton with config read/write and domain mapping"
```

---

## Task 2: ha_setup 工具

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 在 TOOL_SCHEMAS 行之前添加 ha_setup 函数和相关辅助**

```python
import requests as _requests


def _get_ha_client() -> tuple:
    config = _read_config()
    if not config.get("ha_url") or not config.get("ha_token"):
        raise ValueError("未配置 Home Assistant，请先使用 ha_setup 工具连接")
    return config["ha_url"], config["ha_token"]


def _check_ha_connection(ha_url: str, ha_token: str) -> dict:
    resp = _requests.get(
        f"{ha_url}/api/",
        headers={"Authorization": f"Bearer {ha_token}"},
        timeout=10,
    )
    if resp.status_code != 200:
        raise ValueError(f"HA 连接失败: HTTP {resp.status_code}")
    return resp.json()


def ha_setup(ha_url: str = None, ha_token: str = None, **kwargs) -> dict:
    if not ha_url and not ha_token:
        config = _read_config()
        if not config.get("ha_url"):
            return {"connected": False, "error": "未配置 Home Assistant"}
        try:
            info = _check_ha_connection(config["ha_url"], config["ha_token"])
            return {
                "connected": True,
                "ha_url": config["ha_url"],
                "version": info.get("ha_version", info.get("message", "unknown")),
                "triggers": config.get("triggers", []),
            }
        except Exception as e:
            return {"connected": False, "error": f"无法连接到 Home Assistant: {e}"}

    if not ha_url or not ha_token:
        return {"success": False, "error": "ha_url 和 ha_token 必须同时提供"}

    try:
        info = _check_ha_connection(ha_url, ha_token)
    except Exception as e:
        return {"connected": False, "error": f"无法连接到 Home Assistant: {e}"}

    def _update(config):
        config["ha_url"] = ha_url
        config["ha_token"] = ha_token
        if "triggers" not in config:
            config["triggers"] = []
        return config

    _atomic_update(_update)

    try:
        from niu_api.internal.ha_watcher import start_watcher
        start_watcher()
    except ImportError:
        pass

    return {
        "connected": True,
        "ha_url": ha_url,
        "version": info.get("ha_version", info.get("message", "unknown")),
    }
```

- [ ] **Step 2: 填充 TOOL_SCHEMAS 中的 ha_setup**

```python
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
}
```

- [ ] **Step 3: 验证**

```bash
cd mcp-servers/ha-server && PYTHONPATH=src python -c "
from niu_ha_server import ha_setup, get_tool_schemas
result = ha_setup()
print('无参数:', result)
schemas = get_tool_schemas()
print('schemas:', [s['name'] for s in schemas])
"
```

Expected: `无参数: {'connected': False, 'error': '未配置 Home Assistant'}`, `schemas: ['ha_setup']`

- [ ] **Step 4: 提交**

```bash
git add mcp-servers/ha-server/
git commit -m "feat: implement ha_setup tool"
```

---

## Task 3: ha_status 工具

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 添加 WebSocket 短连接辅助函数**

在 `_check_ha_connection` 函数之后添加：

```python
import asyncio as _asyncio


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
```

- [ ] **Step 2: 添加 ha_status 工具函数**

```python
def ha_status(area: str = None, domain: str = None, **kwargs) -> dict:
    try:
        ha_url, ha_token = _get_ha_client()
    except ValueError as e:
        return {"connected": False, "error": str(e)}

    try:
        states_resp = _requests.get(
            f"{ha_url}/api/states",
            headers={"Authorization": f"Bearer {ha_token}"},
            timeout=30,
        )
        if states_resp.status_code != 200:
            return {"connected": False, "error": f"获取状态失败: HTTP {states_resp.status_code}"}
        states = states_resp.json()

        devices = _ws_call(ha_url, ha_token, {"id": 1, "type": "config/device_registry/list"}) or []
        areas = _ws_call(ha_url, ha_token, {"id": 2, "type": "config/area_registry/list"}) or []
    except Exception as e:
        return {"connected": False, "error": f"查询失败: {e}"}

    area_map = {a["area_id"]: a["name"] for a in areas}

    # device_id → {name, area} 映射
    device_info = {}
    for dev in devices:
        dev_name = dev.get("name_by_user") or dev.get("name", "")
        dev_area = area_map.get(dev.get("area_id"), "")
        device_info[dev.get("id")] = {"name": dev_name, "area": dev_area}

    # 通过 entity_registry 关联 device_id
    entity_device_map = {}
    try:
        entities = _ws_call(ha_url, ha_token, {"id": 3, "type": "config/entity_registry/list"}) or []
        for ent in entities:
            did = ent.get("device_id")
            if did and did in device_info:
                entity_device_map[ent.get("entity_id", "")] = device_info[did]
    except Exception:
        pass

    domain_filter = domain
    area_filter = area

    result_devices = []
    result_scenes = []
    result_automations = []
    result_areas = [{"id": a["area_id"], "name": a["name"]} for a in areas]

    for entity in states:
        eid = entity.get("entity_id", "")
        ent_domain = eid.split(".")[0]

        if ent_domain in EXCLUDED_DOMAINS:
            continue

        if domain_filter and ent_domain != domain_filter:
            continue

        info = DOMAIN_MAP.get(ent_domain)
        if not info:
            continue

        attrs = entity.get("attributes", {})
        name = attrs.get("friendly_name", eid)
        state = entity.get("state", "")

        dev_info = entity_device_map.get(eid, {})
        area_name = dev_info.get("area", "")

        if area_filter and area_filter not in name and area_filter not in area_name:
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
```

- [ ] **Step 3: 在 TOOL_SCHEMAS 中注册 ha_status**

在 `TOOL_SCHEMAS` 字典中添加：

```python
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
```

- [ ] **Step 4: 提交**

```bash
git add mcp-servers/ha-server/
git commit -m "feat: implement ha_status tool with area/domain filters"
```

---

## Task 4: ha_control 工具

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 添加 ha_control 工具函数**

```python
def ha_control(entity_id: str, action: str, value: float = None, **kwargs) -> dict:
    try:
        ha_url, ha_token = _get_ha_client()
    except ValueError as e:
        return {"success": False, "error": str(e)}

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
        service_data["brightness"] = int(value * 2.55)
    elif action == "set_temperature" and value is not None:
        service_data["temperature"] = value

    try:
        resp = _requests.post(
            f"{ha_url}/api/services/{service}",
            headers={"Authorization": f"Bearer {ha_token}"},
            json=service_data,
            timeout=15,
        )
        if resp.status_code != 200:
            return {"success": False, "error": f"服务调用失败: HTTP {resp.status_code}"}

        try:
            changed = resp.json()
        except _requests.exceptions.JSONDecodeError:
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
            f"{ha_url}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {ha_token}"},
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
```

- [ ] **Step 2: 在 TOOL_SCHEMAS 中注册 ha_control**

```python
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
```

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/ha-server/
git commit -m "feat: implement ha_control tool with domain-action validation"
```

---

## Task 5: ha_subscribe 工具

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 添加 ha_subscribe 工具函数**

```python
def ha_subscribe(entity_id: str = None, condition: str = None, value: float = None,
                 from_state: str = None, to_state: str = None,
                 description: str = None, trigger_id: str = None,
                 operation: str = None, **kwargs) -> dict:
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
    if condition in ("above", "below") and value is None:
        return {"success": False, "error": f"condition 为 {condition} 时 value 必填"}

    if not description:
        description = f"{entity_id} {condition} {value or ''}".strip()

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
    return {
        "success": True,
        "trigger_id": result.get("trigger_id", ""),
        "message": f"已订阅: {description}",
    }
```

- [ ] **Step 2: 在 TOOL_SCHEMAS 中注册 ha_subscribe**

```python
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
```

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/ha-server/
git commit -m "feat: implement ha_subscribe tool with list/unsubscribe modes"
```

---

## Task 6: ha_integrate 工具 + MCP stdio 入口

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 添加 _parse_data_schema 和 ha_integrate 函数**

```python
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


def ha_integrate(handler: str = None, flow_id: str = None, data: dict = None,
                 operation: str = None, entry_id: str = None, **kwargs) -> dict:
    try:
        ha_url, ha_token = _get_ha_client()
    except ValueError as e:
        return {"success": False, "error": str(e)}

    headers = {"Authorization": f"Bearer {ha_token}", "Content-Type": "application/json"}

    if operation == "delete":
        if not entry_id:
            return {"success": False, "error": "删除集成时 entry_id 必填"}
        resp = _requests.delete(
            f"{ha_url}/api/config/config_entries/entry/{entry_id}",
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
                f"{ha_url}/api/config/config_entries/flow",
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
                f"{ha_url}/api/config/config_entries/flow/{flow_id}",
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
```

- [ ] **Step 2: 在 TOOL_SCHEMAS 中注册 ha_integrate**

```python
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
```

- [ ] **Step 3: 添加 run_server 函数（文件末尾）**

```python
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
```

- [ ] **Step 4: 提交**

```bash
git add mcp-servers/ha-server/
git commit -m "feat: implement ha_integrate tool and MCP stdio entry point"
```

---

## Task 7: HAWatcher 守护线程

**Files:**
- Create: `niu_api/internal/ha_watcher/__init__.py`
- Create: `niu_api/internal/ha_watcher/watcher.py`

- [ ] **Step 1: 创建 __init__.py**

```python
"""HAWatcher — Home Assistant WebSocket 守护线程，条件触发推送。"""
from niu_api.internal.ha_watcher.watcher import start_watcher, stop_watcher, check_and_start

__all__ = ["start_watcher", "stop_watcher", "check_and_start"]
```

- [ ] **Step 2: 创建 watcher.py**

```python
"""HAWatcher 守护线程 — WebSocket 长连接 + subscribe_trigger + ChatQueue 推送。"""

import asyncio
import json
import os
import threading
import time

_watcher = None
_init_lock = threading.Lock()
CONFIG_PATH = os.path.expanduser("~/.niu/ha-config.json")


def start_watcher():
    global _watcher
    with _init_lock:
        if _watcher is not None:
            _watcher.stop()
        _watcher = _HAWatcher()
        _watcher.start()


def stop_watcher():
    global _watcher
    with _init_lock:
        if _watcher is not None:
            _watcher.stop()
            _watcher = None


def check_and_start():
    if not os.path.exists(CONFIG_PATH):
        return
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        if config.get("ha_url") and config.get("ha_token"):
            start_watcher()
    except Exception:
        pass


class _HAWatcher:
    def __init__(self):
        self._thread = None
        self._running = False
        self._last_mtime = 0
        self._current_subscriptions = {}

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        while self._running:
            try:
                loop.run_until_complete(self._connect_and_listen())
            except Exception as e:
                print(f"[HAWatcher] 连接异常: {e}, 5秒后重连...")
                time.sleep(5)

    async def _connect_and_listen(self):
        import websockets

        config = self._read_config()
        if not config or not config.get("ha_url"):
            self._wait_for_config_change(timeout=30)
            return

        ha_url = config["ha_url"]
        ha_token = config.get("ha_token", "")
        triggers = config.get("triggers", [])

        if not triggers:
            self._wait_for_config_change(timeout=30)
            return

        ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"

        async with websockets.connect(ws_url, max_size=5_000_000) as ws:
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_required":
                raise ValueError(f"Unexpected: {msg}")

            await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise ValueError(f"HA 认证失败: {msg}")

            self._current_subscriptions = {}
            msg_id = 1
            for trigger in triggers:
                msg_id += 1
                trigger_config = self._build_trigger_config(trigger)
                await ws.send(json.dumps({
                    "id": msg_id,
                    "type": "subscribe_trigger",
                    "trigger": trigger_config,
                }))
                result = json.loads(await ws.recv())
                if result.get("success"):
                    self._current_subscriptions[trigger["id"]] = msg_id

            # 订阅完成后记录当前 mtime，避免立即重连
            try:
                self._last_mtime = os.path.getmtime(CONFIG_PATH)
            except OSError:
                pass

            last_ping = time.time()
            while self._running:
                if self._check_config_changed():
                    return

                if time.time() - last_ping > 30:
                    msg_id += 1
                    await ws.send(json.dumps({"id": msg_id, "type": "ping"}))
                    last_ping = time.time()

                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                    if msg.get("type") == "event":
                        self._handle_trigger_event(msg, triggers)
                except asyncio.TimeoutError:
                    continue

    def _build_trigger_config(self, trigger: dict) -> dict:
        condition = trigger.get("condition", "state_change")
        entity_id = trigger.get("entity_id", "")

        if condition == "state_change":
            config = {"platform": "state", "entity_id": entity_id}
            if "from_state" in trigger:
                config["from"] = trigger["from_state"]
            if "to_state" in trigger:
                config["to"] = trigger["to_state"]
            return config
        elif condition == "above":
            return {"platform": "numeric_state", "entity_id": entity_id, "above": trigger.get("threshold", 0)}
        elif condition == "below":
            return {"platform": "numeric_state", "entity_id": entity_id, "below": trigger.get("threshold", 0)}
        return {"platform": "state", "entity_id": entity_id}

    def _handle_trigger_event(self, msg: dict, triggers: list):
        event = msg.get("event", {})
        sub_id = msg.get("id")  # subscribe_trigger 返回的 msg_id

        # 通过 msg_id 查找 trigger_id，再查找描述
        trigger_id = None
        for tid, mid in self._current_subscriptions.items():
            if mid == sub_id:
                trigger_id = tid
                break

        description = ""
        for t in triggers:
            if t.get("id") == trigger_id:
                description = t.get("description", f"{t.get('entity_id', '')} 状态变化")
                break

        if not description:
            trigger_data = event.get("variables", {}).get("trigger", {})
            entity_id = trigger_data.get("entity_id", "")
            description = f"{entity_id} 状态变化"

        self._push_to_chat(description)

    def _push_to_chat(self, description: str):
        try:
            from niu_api.chat_queue import get_chat_queue
            q = get_chat_queue()
            q.enqueue_sync(
                content=f"[智能家居] {description}",
                source="ha-watcher",
                channel="ha",
                session_id="default",
            )
        except Exception as e:
            print(f"[HAWatcher] 推送失败: {e}")

    def _read_config(self) -> dict:
        try:
            if not os.path.exists(CONFIG_PATH):
                return {}
            with open(CONFIG_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def _check_config_changed(self) -> bool:
        # 先检查 Event（并立即清除），避免 mtime + event 双重触发
        try:
            from niu_ha_server import _config_event
            if _config_event.is_set():
                _config_event.clear()
                return True
        except ImportError:
            pass

        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                return True
        except OSError:
            pass

        return False

    def _wait_for_config_change(self, timeout: float = 30):
        deadline = time.time() + timeout
        while self._running and time.time() < deadline:
            if self._check_config_changed():
                return
            time.sleep(2)
```

- [ ] **Step 3: 验证模块可导入**

```bash
cd <repo_root> && PYTHONPATH=niu_api python -c "
from niu_api.internal.ha_watcher import check_and_start, start_watcher, stop_watcher
print('OK: HAWatcher module loaded')
"
```

Expected: `OK: HAWatcher module loaded`

- [ ] **Step 4: 提交**

```bash
git add niu_api/internal/ha_watcher/
git commit -m "feat: implement HAWatcher daemon thread with subscribe_trigger and ChatQueue push"
```

---

## Task 8: 注册 ha-server 到系统

**Files:**
- Modify: `config/mcp-servers.yaml`
- Modify: `agent/mcp_loader.py`

- [ ] **Step 1: 在 mcp-servers.yaml 添加 ha-server 配置**

在现有服务器列表末尾添加：

```yaml
ha-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_ha_server"
  workdir: mcp-servers/ha-server/src
  optional: true
  tools:
    ha_status:
      visibility: hidden
    ha_control:
      visibility: hidden
    ha_subscribe:
      visibility: hidden
    ha_setup:
      visibility: hidden
    ha_integrate:
      visibility: hidden
```

- [ ] **Step 2: 在 mcp_loader.py 添加 ha-server**

ha-server 是可选功能（非所有用户都有 Home Assistant），添加到 `OPTIONAL_SERVERS` 而非 `REQUIRED_SERVERS`：

```python
OPTIONAL_SERVERS: List[Tuple[str, str]] = [
    ("feishu-server", "niu_feishu_server"),
    ("ha-server", "niu_ha_server"),
]
```

同时确认 mcp-servers.yaml 中有 `optional: true`。

- [ ] **Step 3: 在 niu_api 启动时初始化 HAWatcher，关闭时停止**

找到 niu_api 启动入口中 scheduler 的 `signal_scheduler_ready()` 或 `start_scheduler()` 附近，添加：

```python
try:
    from niu_api.internal.ha_watcher import check_and_start
    check_and_start()
except Exception as e:
    print(f"[HA] 启动检查失败: {e}")
```

在 niu_api 关闭流程中（`stop_scheduler()` 或 `shutdown` 函数附近），添加：

```python
try:
    from niu_api.internal.ha_watcher import stop_watcher
    stop_watcher()
except Exception as e:
    print(f"[HA] 停止失败: {e}")
```

- [ ] **Step 4: 验证注册成功**

```bash
cd <repo_root> && PYTHONPATH=mcp-servers/ha-server/src python -c "
from niu_ha_server import get_tool_schemas
schemas = get_tool_schemas()
print(f'已注册 {len(schemas)} 个工具:')
for s in schemas:
    print(f'  - {s[\"name\"]}')
"
```

Expected:
```
已注册 5 个工具:
  - ha_setup
  - ha_status
  - ha_control
  - ha_subscribe
  - ha_integrate
```

- [ ] **Step 5: 提交**

```bash
git add config/mcp-servers.yaml agent/mcp_loader.py niu_api/
git commit -m "feat: register ha-server in mcp_loader and add HAWatcher to niu_api startup"
```

---

## Task 9: 端到端验证

**Files:** 无新文件

- [ ] **Step 1: 启动程序，验证 ha-server 加载**

```bash
cd <repo_root> && ./niu
```

确认日志中 ha-server 加载成功，5 个工具注册成功。

- [ ] **Step 2: 验证 ha_setup**

向 Agent 说："连接 Home Assistant，地址 http://localhost:8123，token 是 xxx"

确认 `~/.niu/ha-config.json` 已创建，权限 600，HAWatcher 启动成功。

- [ ] **Step 3: 验证 ha_status**

向 Agent 说："查询所有智能家居设备"

确认返回设备列表、场景、自动化。

- [ ] **Step 4: 验证 ha_control**

向 Agent 说："把书房灯关了"

确认灯关闭。

- [ ] **Step 5: 验证 ha_subscribe**

向 Agent 说："温度超过 25 度提醒我"

确认 ha-config.json 中 triggers 列表新增，HAWatcher 订阅成功。

- [ ] **Step 6: 验证 ha_integrate**

向 Agent 说："添加小米集成"

确认返回配置流表单字段。

- [ ] **Step 7: 提交最终验证**

```bash
git add -A && git commit -m "feat: complete smart home integration (ha-server + HAWatcher)"
```
