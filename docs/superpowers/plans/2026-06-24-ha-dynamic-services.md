# HA 动态服务发现 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ha_control 从硬编码 action 映射改为动态获取 HA 可用服务，让 Agent 能调用每个设备的全部能力（如扫地机的 start/pause/return_to_base/clean_spot/set_fan_speed）。

**Architecture:** 在 ha_setup 连接成功时调用 `GET /api/services` 缓存完整服务列表到 `~/.niu/ha-services.json`。ha_status 根据实体的 domain + `supported_features`/`supported_color_modes` 等属性动态计算可用 actions。ha_control 直接调用 `domain/service` 而非通过硬编码映射，支持任意 service + service_data 参数。

**Tech Stack:** Python, HA REST API (`GET /api/services`, `POST /api/services/{domain}/{service}`), requests

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `mcp-servers/ha-server/src/niu_ha_server/__init__.py` | 唯一修改文件：服务缓存、动态 actions、通用 ha_control |
| `~/.niu/ha-services.json` | 新增缓存文件：HA 服务列表（ha_setup 时写入） |
| `config/disk/ha-server.yaml` | 更新 ha_control 参数描述 |

## 关键设计决策

### 1. 服务缓存策略

- **缓存时机**：`ha_setup` 连接成功时调用 `GET /api/services` 并写入 `~/.niu/ha-services.json`
- **缓存格式**：`{domain: {service_name: {description, fields: {field_name: {required, description, example, selector}}}}}`
- **缓存读取**：`ha_status` 和 `ha_control` 从缓存读取，不每次调 API
- **缓存刷新**：`ha_setup` 重新连接时自动刷新；缓存文件不存在时降级到硬编码 DOMAIN_MAP
- **为什么不用 supported_features 位掩码**：位掩码是 HA 内部实现细节，不同版本可能变化。`GET /api/services` 返回的是 HA 实际注册的服务，更可靠。但 `supported_features` 仍用于过滤——如果 domain 有 `start` 服务但实体的 `supported_features` 不含 START 位，则该 action 不显示。

### 2. 动态 actions 计算逻辑

对每个实体，可用 actions = domain 可用服务 ∩ 实体能力支持的服务：

```
entity_actions = []
for service_name in cached_services[domain]:
    if _entity_supports_service(entity_attrs, domain, service_name):
        entity_actions.append(service_name)
```

`_entity_supports_service` 的判断规则：
- **通用服务**（turn_on, turn_off, toggle）：所有 domain 都支持，直接返回 True
- **domain 特有服务**：检查 `supported_features` 位掩码或属性列表
  - vacuum: `supported_features` 位掩码（START=16384, PAUSE=4, STOP=8, RETURN=16, SET_FAN_SPEED=32, CLEAN_SPOT=256, LOCATE=512）
  - light: `supported_color_modes` 包含 "color_temp" 或 "rgb" → 支持 brightness/color 控制
  - climate: `hvac_modes` 非空 → 支持 set_hvac_mode；`preset_modes` 非空 → 支持 set_preset_mode
  - fan: `supported_features` 含 SET_SPEED(4) → 支持 set_percentage；`preset_modes` 非空 → 支持 set_preset_mode
  - cover: `supported_features` 含 SET_POSITION(4) → 支持 set_cover_position；含 SET_TILT(8) → 支持 set_cover_tilt_position
  - humidifier: `available_modes` 非空 → 支持 set_mode
  - media_player: `source_list` 非空 → 支持 select_source；`volume_level` 存在 → 支持 volume_set
- **无位掩码的 domain**（switch, lock, scene, script, automation）：所有该 domain 的服务都支持

### 3. ha_control 通用化

- **参数**：`entity_id`（必填）+ `service`（必填，替代原 `action`）+ `service_data`（可选，替代原 `value`）
- **向后兼容**：`action` 参数仍接受，内部映射到 `service`（如 `action="turn_on"` → `service="turn_on"`，`action="set_brightness"` → `service="turn_on"` + `service_data={"brightness_pct": value}`）
- **行为变更**：`set_brightness` 旧代码发送 `brightness`（0-255，乘 2.55），新代码发送 `brightness_pct`（0-100，直接透传）。`brightness_pct` 是 HA 推荐的 API 参数，实际效果与旧代码一致（value=50 → 旧代码 brightness=127，新代码 brightness_pct=50 → HA 内部也转为 127）
- **互斥规则**：`service` 和 `action` 不应同时传入。若同时传入，`service` 优先，`action` 仅用于 value 兼容处理（此时 action 的 value 兼容逻辑**不会执行**，避免错误参数注入）
- **domain 特殊映射**：vacuum 域没有 `turn_on`/`turn_off` 服务，当 action="turn_on" 用于 vacuum 实体时，映射到 `start`；action="turn_off" 映射到 `return_to_base`
- **service_data 透传**：直接作为 `POST /api/services/{domain}/{service}` 的 `json` body 中的额外字段（与 `entity_id` 合并）
- **action → service 映射表**（向后兼容层，仅当传入 `action` 时使用）：

| action | service | service_data |
|--------|---------|-------------|
| turn_on | turn_on（vacuum→start） | {} |
| turn_off | turn_off（vacuum→return_to_base） | {} |
| toggle | toggle | {} |
| set_brightness | turn_on | {"brightness_pct": value} |
| set_temperature | set_temperature | {"temperature": value} |
| open | open_cover | {} |
| close | close_cover | {} |
| lock | lock | {} |
| unlock | unlock | {} |
| activate | turn_on (scene) | {} |
| run | turn_on (script) | {} |
| trigger | trigger (automation) | {} |

### 4. ha_status 返回格式变化

每个设备的 `actions` 字段从硬编码列表变为动态计算的服务列表。同时新增 `services` 字段，包含每个服务的参数定义：

```json
{
  "name": "扫地机器人",
  "entity_id": "vacuum.18603118098",
  "type": "扫地机",
  "state": "docked",
  "actions": ["start", "pause", "stop", "return_to_base", "clean_spot", "locate", "set_fan_speed"],
  "services": {
    "start": {"fields": {}},
    "pause": {"fields": {}},
    "set_fan_speed": {"fields": {"fan_speed": {"required": true, "options": ["quiet", "normal", "max", "max_plus"]}}}
  },
  "properties": {
    "fan_speed": "normal",
    "fan_speed_list": ["quiet", "normal", "max", "max_plus"],
    "rooms": {"chu_fang": 0, "ke_wei": 2, ...}
  }
}
```

**`services` 字段省略规则**：当服务无参数字段时，`services` 中只列服务名不展开 fields（减少返回体积）。只有有参数的服务才展开 fields。无参数的服务通过 `actions` 列表即可知道可用。

**`properties` 扩展**：ATTR_WHITELIST 扩展，增加 `fan_speed_list`（vacuum）、`rooms`（vacuum）、`hvac_modes`（climate）、`preset_modes`（climate/fan/humidifier）、`source_list`（media_player）等属性，让 Agent 知道可选值。

---

## Task 1: 服务缓存机制

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:17-19` (新增常量)
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:346-394` (ha_setup 函数)

- [ ] **Step 1: 添加服务缓存路径常量和读写函数**

在 `CONFIG_PATH` 定义之后添加：

```python
SERVICES_CACHE_PATH = os.path.expanduser("~/.niu/ha-services.json")
```

在 `_write_config` 函数之后添加：

```python
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
                                        o if isinstance(o, str) else o.get("value", str(o))
                                        for o in opts
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
```

- [ ] **Step 2: 在 ha_setup 连接成功后调用 _fetch_and_cache_services**

在 `ha_setup` 函数中，连接成功后（`conn["connected"] = True` 的两个分支）添加服务缓存调用。

**分支 1**（传入 ha_url + ha_token，约第 351-371 行）：

在 `result = _atomic_update(_setup)` 之后、`try: from niu_api.internal...` 之前添加：

```python
        # 缓存 HA 服务列表
        _fetch_and_cache_services(ha_url, headers)
```

**分支 2**（无参数，查询当前状态，约第 373-394 行）：

在 `conn = _check_ha_connection(url, headers)` 成功后（`if not conn["connected"]: return conn` 之后）添加：

```python
    # 刷新服务缓存（如果缓存为空则获取，否则跳过）
    if not _read_services_cache():
        _fetch_and_cache_services(url, headers)
```

- [ ] **Step 3: 验证缓存功能**

运行测试命令，确认 `~/.niu/ha-services.json` 被正确创建：

```bash
python3 -c "
from mcp_servers_ha_server import ha_setup
# 测试无参数调用（应刷新缓存）
result = ha_setup()
print('connected:', result.get('connected'))
import json, os
cache_path = os.path.expanduser('~/.niu/ha-services.json')
if os.path.exists(cache_path):
    with open(cache_path) as f:
        cache = json.load(f)
    print('Cached domains:', sorted(cache.keys()))
    print('vacuum services:', sorted(cache.get('vacuum', {}).keys()))
else:
    print('Cache file not created')
"
```

Expected: `connected: True`, `vacuum services` 包含 `start, pause, stop, return_to_base, clean_spot, locate, set_fan_speed`

- [ ] **Step 4: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): add services cache mechanism for dynamic service discovery"
```

---

## Task 2: 动态 actions 计算

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:66-81` (DOMAIN_MAP 保留但降级为 fallback)
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:83-116` (ATTR_WHITELIST 扩展)
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:239-341` (ha_status 函数)

- [ ] **Step 1: 扩展 ATTR_WHITELIST，增加能力属性**

在 `ATTR_WHITELIST` 中为各 domain 增加能力判断所需的属性：

```python
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
```

- [ ] **Step 2: 添加 _entity_supports_service 函数**

在 `_fetch_and_cache_services` 函数之后添加：

```python
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
    """判断实体是否支持某个服务。基于 supported_features 位掩码和属性列表。
    仅在缓存中已有该 domain 的服务列表时被调用（_get_entity_actions 保证）。"""
    sf = attrs.get("supported_features")

    # 通用服务：所有 domain 都支持
    if service in ("turn_on", "turn_off", "toggle"):
        return True

    # domain 特有判断
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
        # send_command 等无位掩码的服务：无法从 supported_features 判断，默认显示
        # （调用时若 HA 报错，用户会看到错误信息）
        return True

    if domain == "light":
        if service == "turn_on":
            # turn_on 本身总是支持，但 brightness/color 控制需要 color_modes
            return True
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
            return isinstance(sf, int) and bool(sf & FAN_PRESET_MODE)
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
            return isinstance(sf, int) and bool(sf & MEDIA_SELECT_SOURCE) or bool(attrs.get("source_list"))
        return True

    # 其他 domain（switch, lock, scene, script, automation）：所有服务都支持
    return True
```

- [ ] **Step 3: 添加 _get_entity_actions 函数**

在 `_entity_supports_service` 之后添加：

```python
def _get_entity_actions(domain: str, attrs: dict, services_cache: dict) -> list:
    """根据服务缓存和实体属性计算可用 actions 列表。
    缓存存在时从缓存+位掩码动态计算；缓存为空时降级到 DOMAIN_MAP。"""
    domain_services = services_cache.get(domain, {})
    if not domain_services:
        # 降级到硬编码 DOMAIN_MAP（注意：vacuum 的 turn_on/turn_off
        # 在 HA 中不存在，降级场景下 vacuum 控制可能不可用）
        info = DOMAIN_MAP.get(domain, {})
        return info.get("actions", [])

    actions = []
    for svc_name in sorted(domain_services.keys()):
        if _entity_supports_service(attrs, domain, svc_name):
            actions.append(svc_name)
    return actions
```

- [ ] **Step 4: 修改 ha_status 使用动态 actions**

在 `ha_status` 函数中，替换硬编码 `info["actions"]` 为动态计算。

在 `ha_status` 函数开头（`config = _read_config()` 之后）添加：

```python
    services_cache = _read_services_cache()
```

将第 309-316 行的 entry 构建替换为：

```python
        entry = {
            "name": name,
            "area": area_name,
            "entity_id": eid,
            "type": info["type"] if info else domain,
            "state": state,
            "actions": _get_entity_actions(ent_domain, attrs, services_cache),
        }
```

同时，在 entry 构建之后、properties 提取之前，添加 services 字段（仅包含有参数的服务）：

```python
        # 添加有参数的服务定义（用实体属性覆盖 domain 级别选项）
        entity_services = {}
        domain_svcs = services_cache.get(ent_domain, {})
        for act in entry["actions"]:
            if act in domain_svcs:
                fields = domain_svcs[act].get("fields", {})
                if fields:
                    # 用实体属性覆盖缓存中的选项（实体级 > domain级）
                    svc_def = {"fields": {k: dict(v) for k, v in fields.items()}}  # 两级拷贝，避免修改缓存
                    for fname, finfo in svc_def["fields"].items():
                        # vacuum.set_fan_speed.fan_speed → 用实体的 fan_speed_list
                        if ent_domain == "vacuum" and fname == "fan_speed" and "fan_speed_list" in attrs:
                            finfo["options"] = attrs["fan_speed_list"]
                        # climate.set_hvac_mode.hvac_mode → 用实体的 hvac_modes
                        elif ent_domain == "climate" and fname == "hvac_mode" and "hvac_modes" in attrs:
                            finfo["options"] = attrs["hvac_modes"]
                        # climate.set_preset_mode.preset_mode → 用实体的 preset_modes
                        elif ent_domain == "climate" and fname == "preset_mode" and "preset_modes" in attrs:
                            finfo["options"] = attrs["preset_modes"]
                        # humidifier.set_mode.mode → 用实体的 available_modes
                        elif ent_domain == "humidifier" and fname == "mode" and "available_modes" in attrs:
                            finfo["options"] = attrs["available_modes"]
                        # fan.set_preset_mode.preset_mode → 用实体的 preset_modes
                        elif ent_domain == "fan" and fname == "preset_mode" and "preset_modes" in attrs:
                            finfo["options"] = attrs["preset_modes"]
                        # media_player.select_source.source → 用实体的 source_list
                        elif ent_domain == "media_player" and fname == "source" and "source_list" in attrs:
                            finfo["options"] = attrs["source_list"]
                    entity_services[act] = svc_def
        if entity_services:
            entry["services"] = entity_services
```

- [ ] **Step 5: 验证动态 actions**

```bash
python3 -c "
import json, os, sys
sys.path.insert(0, 'mcp-servers/ha-server/src')
from niu_ha_server import ha_status

result = ha_status(domain='vacuum')
print(json.dumps(result, ensure_ascii=False, indent=2))
"
```

Expected: vacuum 设备的 `actions` 包含 `start, pause, stop, return_to_base, clean_spot, locate, set_fan_speed`，`services` 包含 `set_fan_speed` 的 fields 定义（含 fan_speed 选项），`properties` 包含 `fan_speed_list` 和 `rooms`。

- [ ] **Step 6: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): dynamic actions based on HA services cache and entity capabilities"
```

---

## Task 3: ha_control 通用化

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:123-136` (ACTION_SERVICE_MAP 保留为兼容层)
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:399-474` (ha_control 函数)
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py:711-723` (TOOL_SCHEMAS)

- [ ] **Step 1: 添加向后兼容的 action → service 映射**

在 `ACTION_SERVICE_MAP` 之后添加：

```python
# 向后兼容：旧 action 参数到 service + service_data 的映射
# 注意：vacuum 域没有 turn_on/turn_off，需要 domain 特殊处理（在 ha_control 中实现）
ACTION_COMPAT_MAP = {
    "turn_on": "turn_on",
    "turn_off": "turn_off",
    "toggle": "toggle",
    "set_brightness": "turn_on",  # brightness_pct 由 value 参数注入
    "set_temperature": "set_temperature",  # temperature 由 value 参数注入
    "open": "open_cover",
    "close": "close_cover",
    "lock": "lock",
    "unlock": "unlock",
    "activate": "turn_on",  # scene domain
    "run": "turn_on",  # script domain
    "trigger": "trigger",  # automation domain
}

# vacuum 域的 action 特殊映射（vacuum 没有 turn_on/turn_off）
VACUUM_ACTION_MAP = {
    "turn_on": "start",
    "turn_off": "return_to_base",
}
```

- [ ] **Step 2: 重写 ha_control 函数**

替换整个 `ha_control` 函数（第 399-474 行）：

```python
def ha_control(entity_id: str, action: str = "", service: str = "",
               value: float = None, service_data: dict = None, **kwargs) -> dict:
    """控制智能家居设备。支持两种调用方式：
    1. 新方式：service + service_data（直接调用 HA 服务，如 service="start", service_data={}）
    2. 旧方式：action + value（向后兼容，如 action="turn_on"）
    service 和 action 不应同时传入；若同时传入，service 优先，action 的 value 兼容逻辑不执行。"""
    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"success": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

    domain = entity_id.split(".")[0]
    _used_action_compat = False  # 标记是否使用了 action 兼容模式（用于 value 处理）

    # 解析 service：优先用 service 参数，否则从 action 映射
    if not service and action:
        _used_action_compat = True
        # vacuum 域特殊映射
        if domain == "vacuum" and action in VACUUM_ACTION_MAP:
            service = VACUUM_ACTION_MAP[action]
        else:
            compat = ACTION_COMPAT_MAP.get(action)
            if compat:
                service = compat
            else:
                # action 不在兼容映射中，尝试直接作为 service 名
                service = action

    if not service:
        return {"success": False, "error": "必须提供 service 或 action 参数"}

    # 构建 service_data
    data = {"entity_id": entity_id}

    if service_data:
        # 新方式：service_data 直接合并
        data.update(service_data)

    # value 参数兼容处理：仅在 action 兼容模式下执行（service 模式下不执行，避免错误参数注入）
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
    if domain_services and service not in domain_services:
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

    # 调用 HA API
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

        # 回退查询当前状态
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
```

- [ ] **Step 3: 更新 TOOL_SCHEMAS 中的 ha_control 定义**

替换 `TOOL_SCHEMAS` 中的 `ha_control` 条目（第 711-723 行）：

```python
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
```

- [ ] **Step 4: 验证 ha_control 通用化**

测试扫地机的 start 服务（注意：这会实际启动扫地机，请确认可以测试）：

```bash
python3 -c "
import json, sys
sys.path.insert(0, 'mcp-servers/ha-server/src')
from niu_ha_server import ha_control

# 测试向后兼容（旧 action 方式 — vacuum 的 turn_on 映射到 start）
result = ha_control(entity_id='vacuum.18603118098', action='turn_on')
print('vacuum turn_on (compat → start):', json.dumps(result, ensure_ascii=False))

# 测试新 service 方式 - locate（安全操作，不会启动清扫）
result = ha_control(entity_id='vacuum.18603118098', service='locate')
print('locate (new):', json.dumps(result, ensure_ascii=False))

# 测试 set_fan_speed（有参数的服务）
result = ha_control(entity_id='vacuum.18603118098', service='set_fan_speed', service_data={'fan_speed': 'quiet'})
print('set_fan_speed:', json.dumps(result, ensure_ascii=False))
"
```

Expected: `locate` 返回 `success: True`，`set_fan_speed` 返回 `success: True`。

- [ ] **Step 5: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): generalize ha_control with dynamic service calls and backward compatibility"
```

---

## Task 4: 更新配置文件和 Agent 提示词

**Files:**
- Modify: `config/disk/ha-server.yaml:33-51` (ha_control 参数定义)
- Modify: `config/agents/niu.md` (如果包含 HA 相关提示词)

- [ ] **Step 1: 更新 ha-server.yaml 中的 ha_control 参数**

替换 `ha_control` 的 parameters 部分：

```yaml
  - name: ha_control
    category: write
    short: "控制设备"
    long: "控制智能家居设备。两种方式：1) service + service_data（推荐，如 service=start, service_data={fan_speed: max}）；2) action + value（兼容，如 action=turn_on）。可用 service 从 ha_status 的 actions 列表获取"
    parameters:
      - name: entity_id
        position: 1
        type: string
        required: true
        description: "实体 ID，如 vacuum.xxx"
      - name: action
        position: 2
        type: string
        description: "（兼容）动作名，如 turn_on/turn_off"
      - name: service
        type: string
        description: "HA 服务名，如 start/pause/return_to_base/set_fan_speed"
      - name: value
        type: number
        description: "（兼容）亮度 0-100 或目标温度"
      - name: service_data
        type: object
        cli_format: json
        description: "服务参数键值对，如 {fan_speed: max, temperature: 26}"
```

- [ ] **Step 2: 检查并更新 Agent 提示词**

检查 `config/agents/niu.md` 是否包含 HA 控制相关的硬编码指令（如"扫地机只支持 turn_on/turn_off"），如有则更新为"可用服务从 ha_status 的 actions 列表动态获取"。

```bash
grep -n "ha_control\|扫地机\|vacuum\|turn_on\|turn_off" config/agents/niu.md
```

如果找到硬编码指令，更新为动态描述。如果没有，跳过此步骤。

- [ ] **Step 3: 临时提交**

```bash
git add config/disk/ha-server.yaml config/agents/niu.md
git commit -m "docs(ha): update config and agent prompts for dynamic service discovery"
```

---

## Task 5: 端到端验证

**Files:** 无修改，仅测试

- [ ] **Step 1: 验证完整流程 — ha_setup 缓存服务**

```bash
python3 -c "
import json, sys, os
sys.path.insert(0, 'mcp-servers/ha-server/src')
from niu_ha_server import ha_setup

# 删除旧缓存
cache_path = os.path.expanduser('~/.niu/ha-services.json')
if os.path.exists(cache_path):
    os.remove(cache_path)

# 重新连接（应自动缓存服务）
result = ha_setup()
print('Setup result:', json.dumps(result, ensure_ascii=False, indent=2))

# 验证缓存
with open(cache_path) as f:
    cache = json.load(f)
print('Domains:', len(cache))
print('vacuum services:', sorted(cache.get('vacuum', {}).keys()))
print('light services:', sorted(cache.get('light', {}).keys()))
print('climate services:', sorted(cache.get('climate', {}).keys()))
"
```

- [ ] **Step 2: 验证 ha_status 动态 actions**

```bash
python3 -c "
import json, sys
sys.path.insert(0, 'mcp-servers/ha-server/src')
from niu_ha_server import ha_status

# 查看所有设备
result = ha_status()
for dev in result.get('devices', []):
    eid = dev.get('entity_id', '')
    actions = dev.get('actions', [])
    services = dev.get('services', {})
    props = dev.get('properties', {})
    print(f'{eid}: actions={actions}')
    if services:
        print(f'  services: {list(services.keys())}')
    if props:
        key_props = {k: v for k, v in props.items() if k in ('fan_speed_list', 'hvac_modes', 'preset_modes', 'supported_color_modes', 'source_list', 'rooms')}
        if key_props:
            print(f'  key_props: {key_props}')
"
```

Expected:
- vacuum: actions 包含 `start, pause, stop, return_to_base, clean_spot, locate, set_fan_speed`
- light: actions 包含 `toggle, turn_off, turn_on`（有 color_modes 的灯）
- climate: actions 包含 `set_hvac_mode, set_preset_mode, set_temperature, toggle, turn_off, turn_on`（有 preset_modes 的空调）

- [ ] **Step 3: 验证 ha_control 新旧两种调用方式**

```bash
python3 -c "
import json, sys
sys.path.insert(0, 'mcp-servers/ha-server/src')
from niu_ha_server import ha_control

# 1. 旧方式：action 参数（向后兼容）
result = ha_control(entity_id='light.yeelink_bslamp2_b1ce_light', action='turn_off')
print('turn_off (compat):', json.dumps(result, ensure_ascii=False))

# 2. 新方式：service 参数
result = ha_control(entity_id='light.yeelink_bslamp2_b1ce_light', service='turn_on')
print('turn_on (new):', json.dumps(result, ensure_ascii=False))

# 3. 新方式：service + service_data
result = ha_control(entity_id='light.yeelink_bslamp2_b1ce_light', service='turn_on', service_data={'brightness_pct': 50})
print('turn_on + brightness:', json.dumps(result, ensure_ascii=False))

# 4. 扫地机 locate（安全操作）
result = ha_control(entity_id='vacuum.18603118098', service='locate')
print('vacuum locate:', json.dumps(result, ensure_ascii=False))

# 5. 错误：不存在的 service
result = ha_control(entity_id='vacuum.18603118098', service='fly')
print('invalid service:', json.dumps(result, ensure_ascii=False))

# 6. 错误：缺少必填参数
result = ha_control(entity_id='vacuum.18603118098', service='set_fan_speed')
print('missing param:', json.dumps(result, ensure_ascii=False))
"
```

Expected:
- 1-4: `success: True`
- 5: `success: False, error 包含 "不适用于 vacuum"`
- 6: `success: False, error 包含 "缺少必填参数: fan_speed"`

- [ ] **Step 4: 恢复测试中改变的状态**

```bash
python3 -c "
import sys
sys.path.insert(0, 'mcp-servers/ha-server/src')
from niu_ha_server import ha_control
# 恢复灯的状态
ha_control(entity_id='light.yeelink_bslamp2_b1ce_light', action='turn_on')
print('Light restored to on')
"
```

- [ ] **Step 5: 最终提交**

```bash
git add -A
git commit -m "feat(ha): dynamic service discovery — vacuum start/pause/stop/return/clean_spot/fan_speed now available"
```
