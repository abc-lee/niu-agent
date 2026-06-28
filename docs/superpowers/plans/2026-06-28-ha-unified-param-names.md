# HA 参数名统一方案实施计划（v3 — 二次审查修正版）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一 ha-server 所有工具的参数名体系，消除"服务参数名 vs 状态属性名"的二义性。Agent 只需面对服务参数名，程序在入参和出参两个方向都做自动转换。

**Architecture:** 入参方向：`_normalize_scene_entities` 将 Agent 传入的服务参数名转为状态属性名再存入 HA；出参方向：`_denormalize_scene_entities` 将 HA 返回的状态属性名转回服务参数名再返回给 Agent。ATTR_WHITELIST 只使用 REST API 属性名（与 `/api/states` 返回的键名一致）。

**Tech Stack:** Python 3.11+, HA REST API

---

## 审查修正记录

| 轮次 | 问题 | 修正 |
|------|------|------|
| v1→v2 | cover 属性名应为 `current_cover_position` | **v3 再修正**：REST API 属性名是 `current_position`（HA 源码 `ATTR_CURRENT_POSITION = "current_position"`），不是 `current_cover_position`（那是 Python 属性名） |
| v1→v2 | humidifier humidity → target_humidity | **v3 删除**：HA 源码确认 `set_humidity` 服务参数名和 `/api/states` 属性名都是 `humidity`，无需映射 |
| v2→v3 | ha_scene get/list 返回状态属性名与原则矛盾 | 增加 `_denormalize_scene_entities` 反向转换 |
| v2→v3 | attr_name 提示增加认知负担 | 删除 Task 7（attr_name 提示） |
| v2→v3 | snapshot common_keys 设计缺陷 | 删除 common_keys，补充 ATTR_WHITELIST |
| v2→v3 | ATTR_WHITELIST humidifier 中 `target_humidity` 不是 REST API 名 | 删除 |
| v2→v3 | ATTR_WHITELIST fan 中 `speed_list` 不存在 | 删除 |
| v2→v3 | `_reverse_transform` 硬编码问题 | SVC_ATTR_MAP 改为双向映射 `(attr_name, fwd, rev)` |
| v2→v3 | list(detail=true) 两条路径规范不明确 | 明确指出两条路径并给出具体代码 |
| v3→v3.1 | 遗漏 docs/manual-ha-setup.md 更新 | Task 8 增加 Step 3 |
| v3→v3.1 | TOOL_SCHEMAS config 参数 description 没更新 | Task 8 Step 1 同时更新顶层和 config 描述 |
| v3→v3.1 | get 分支需先捕获 resp.json() 再做转换 | Task 6 Step 1 给出具体替换代码 |

## REST API 属性名 vs Python 属性名（HA 源码验证）

| Domain | 服务参数名 | REST API 属性名 | Python 属性名 | 是否需要映射 |
|--------|-----------|----------------|--------------|------------|
| light | `brightness_pct` | `brightness` | `brightness` | 是（名字+值范围不同） |
| cover | `position` | `current_position` | `current_cover_position` | 是（名字不同） |
| cover | `tilt_position` | `current_tilt_position` | `current_cover_tilt_position` | 是（名字不同） |
| humidifier | `humidity` | `humidity` | `target_humidity` | **否**（名字相同） |
| climate | `temperature` | `temperature` | `target_temperature` | 否（名字相同） |

关键发现：HA 开发者文档中列的是 Python 属性名（如 `current_cover_position`、`target_humidity`），但 `/api/states` REST API 返回的键名不同（如 `current_position`、`humidity`）。scene.apply 使用 REST API 属性名。

---

### Task 1: 修复 ATTR_WHITELIST 中的错误

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py` (第 208-249 行)

- [ ] **Step 1: 修复 ATTR_WHITELIST**

当前代码问题：
- `climate` 域：`target_temp_temp` 是笔误，应删除
- `cover` 域：`current_position` / `current_tilt_position` 是正确的 REST API 属性名（已验证），保持不变
- `fan` 域：`speed_list` 不是 REST API 属性名，删除
- `humidifier` 域：`target_humidity` 不是 REST API 属性名（REST API 用 `humidity`），删除
- `light` 域：缺少 `color_temp_kelvin`（snapshot 需要提取）

修改后的 ATTR_WHITELIST：
```python
ATTR_WHITELIST = {
    "climate": [
        "current_temperature", "temperature", "target_temp_high",
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
        "color_temp_kelvin",
    ],
    "switch": [],
    "fan": [
        "percentage", "percentage_step", "preset_modes",
        "direction",
    ],
    "cover": [
        "current_position", "current_tilt_position",
        "supported_features",
    ],
    "lock": [],
    "humidifier": [
        "humidity", "current_humidity", "mode", "available_modes",
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

变更说明：
- `climate`：删除 `target_temp_temp`（笔误）
- `light`：新增 `color_temp_kelvin`（snapshot 需要提取色温）
- `fan`：删除 `speed_list`（不是 REST API 属性名）
- `humidifier`：`target_humidity` → `current_humidity`（`target_humidity` 是 Python 属性名，REST API 返回 `humidity` 表示目标值，`current_humidity` 表示当前实际值）

- [ ] **Step 2: 验证 ATTR_WHITELIST 语法正确**

Run: `cd mcp-servers/ha-server/src && python -c "from niu_ha_server import ATTR_WHITELIST; print('OK:', list(ATTR_WHITELIST.keys()))"`

- [ ] **Step 3: 用真实 HA 验证 ha_status 的 properties 提取正常**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import ha_setup, ha_status
ha_setup()
status = ha_status(domain='light')
for d in status.get('devices', []):
    if d.get('properties'):
        print(f'{d[\"entity_id\"]}: {d[\"properties\"]}')
        break
"`

- [ ] **Step 4: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "fix(ha): correct ATTR_WHITELIST - remove target_temp_temp/speed_list/target_humidity, add color_temp_kelvin/current_humidity"
```

---

### Task 2: 定义 SVC_ATTR_MAP 映射表

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py` (在 ATTR_WHITELIST 之后)

- [ ] **Step 1: 在 ATTR_WHITELIST 之后添加 SVC_ATTR_MAP**

```python
# 服务参数名 ↔ REST API 状态属性名 双向映射
# key: (domain, service_param_name)
# value: (rest_api_attr_name, svc_to_attr_transform, attr_to_svc_transform)
# transform: None=直接映射（名字不同但值相同），callable=值需要转换
SVC_ATTR_MAP = {
    ("light", "brightness_pct"): ("brightness", lambda v: int(round(v * 255 / 100)), lambda v: int(round(v * 100 / 255))),
    ("cover", "position"): ("current_position", None, None),
    ("cover", "tilt_position"): ("current_tilt_position", None, None),
}
```

说明：
- humidifier 不需要映射（`humidity` 在服务和 REST API 中名字相同）
- climate 不需要映射（`temperature` 在服务和 REST API 中名字相同）
- cover 的 `position`（服务参数名）→ `current_position`（REST API 属性名），已通过 HA 源码 `ATTR_CURRENT_POSITION = "current_position"` 验证

- [ ] **Step 2: 验证语法**

Run: `cd mcp-servers/ha-server/src && python -c "from niu_ha_server import SVC_ATTR_MAP; print('SVC_ATTR_MAP:', SVC_ATTR_MAP)"`

- [ ] **Step 3: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): add SVC_ATTR_MAP with HA-source-verified mappings"
```

---

### Task 3: 实现 _normalize_scene_entities 和 _denormalize_scene_entities

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py` (在 SVC_ATTR_MAP 之后)

- [ ] **Step 1: 添加双向转换函数**

```python
def _normalize_scene_entities(entities: dict) -> dict:
    """将 scene entities 中的服务参数名转换为 REST API 状态属性名（入参方向）。

    Agent 传入服务参数名（从 ha_status services 字段获取），
    但 scene config 和 scene.apply 需要 REST API 状态属性名。
    幂等：已是 REST API 属性名的不会被二次转换。
    """
    if not entities:
        return entities
    # 预构建已转换后的属性名集合，用于幂等保护
    _attr_names = {v[0] for v in SVC_ATTR_MAP.values() if v is not None}
    normalized = {}
    for eid, attrs in entities.items():
        if not isinstance(attrs, dict):
            normalized[eid] = attrs
            continue
        domain = eid.split(".")[0] if "." in eid else ""
        new_attrs = {}
        for key, val in attrs.items():
            if key in _attr_names:
                new_attrs[key] = val
                continue
            mapping = SVC_ATTR_MAP.get((domain, key))
            if mapping is not None:
                attr_name, fwd_transform, _ = mapping
                try:
                    new_attrs[attr_name] = fwd_transform(val) if fwd_transform else val
                except (TypeError, ValueError):
                    new_attrs[key] = val
            else:
                new_attrs[key] = val
        normalized[eid] = new_attrs
    return normalized


# 反向映射：REST API 状态属性名 → 服务参数名
_ATTR_SVC_MAP = {}
for _k, _v in SVC_ATTR_MAP.items():
    if _v is not None:
        _ATTR_SVC_MAP[_v[0]] = (_k[0], _k[1], _v[2])  # (domain, svc_name, rev_transform)


def _denormalize_scene_entities(entities: dict) -> dict:
    """将 scene entities 中的 REST API 状态属性名转换回服务参数名（出参方向）。

    ha_scene get/list 返回 HA config API 中的配置，使用 REST API 状态属性名。
    转换回服务参数名，确保 Agent 始终只看到一套名字。
    不在反向映射表中的属性名原样保留。
    """
    if not entities:
        return entities
    denormalized = {}
    for eid, attrs in entities.items():
        if not isinstance(attrs, dict):
            denormalized[eid] = attrs
            continue
        domain = eid.split(".")[0] if "." in eid else ""
        new_attrs = {}
        for key, val in attrs.items():
            if key in _ATTR_SVC_MAP:
                svc_domain, svc_name, rev_transform = _ATTR_SVC_MAP[key]
                if svc_domain == domain:
                    try:
                        new_attrs[svc_name] = rev_transform(val) if rev_transform else val
                    except (TypeError, ValueError):
                        new_attrs[key] = val
                else:
                    new_attrs[key] = val
            else:
                new_attrs[key] = val
        denormalized[eid] = new_attrs
    return denormalized
```

- [ ] **Step 2: 验证双向转换逻辑**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import _normalize_scene_entities, _denormalize_scene_entities

# Test normalize
assert _normalize_scene_entities({'light.x': {'brightness_pct': 50}}) == {'light.x': {'brightness': 128}}
assert _normalize_scene_entities({'cover.x': {'position': 50}}) == {'cover.x': {'current_position': 50}}
assert _normalize_scene_entities({'cover.x': {'tilt_position': 75}}) == {'cover.x': {'current_tilt_position': 75}}
assert _normalize_scene_entities({'climate.x': {'temperature': 26}}) == {'climate.x': {'temperature': 26}}
assert _normalize_scene_entities({'humidifier.x': {'humidity': 60}}) == {'humidifier.x': {'humidity': 60}}

# Test normalize idempotent
assert _normalize_scene_entities({'light.x': {'brightness': 200}}) == {'light.x': {'brightness': 200}}
assert _normalize_scene_entities({'cover.x': {'current_position': 50}}) == {'cover.x': {'current_position': 50}}

# Test denormalize (reverse)
assert _denormalize_scene_entities({'light.x': {'brightness': 128}}) == {'light.x': {'brightness_pct': 50}}
assert _denormalize_scene_entities({'cover.x': {'current_position': 50}}) == {'cover.x': {'position': 50}}
assert _denormalize_scene_entities({'cover.x': {'current_tilt_position': 75}}) == {'cover.x': {'tilt_position': 75}}

# Test denormalize with unmapped attrs preserved
result = _denormalize_scene_entities({'light.x': {'brightness': 128, 'state': 'on', 'color_temp_kelvin': 4000}})
assert result == {'light.x': {'brightness_pct': 50, 'state': 'on', 'color_temp_kelvin': 4000}}, f'Got: {result}'

# Test round-trip
original = {'light.desk': {'brightness_pct': 75, 'state': 'on', 'color_temp_kelvin': 4000}}
normalized = _normalize_scene_entities(original)
denormalized = _denormalize_scene_entities(normalized)
assert denormalized == original, f'Round-trip failed: {denormalized} != {original}'

# Test edge cases
assert _normalize_scene_entities({}) == {}
assert _normalize_scene_entities(None) is None
assert _denormalize_scene_entities({}) == {}
assert _denormalize_scene_entities(None) is None

print('All bidirectional tests passed!')
"`
Expected: `All bidirectional tests passed!`

- [ ] **Step 3: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): add bidirectional _normalize/_denormalize_scene_entities"
```

---

### Task 4: 在 ha_scene create/update 中调用 _normalize_scene_entities

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 在 create 分支中，config 浅拷贝后、POST 前添加归一化**

在 `config = {**config}` 和 `config.pop("id", None)` 之后添加：
```python
        if "entities" in config:
            config["entities"] = _normalize_scene_entities(config["entities"])
```

- [ ] **Step 2: 在 update 分支中做同样的修改**

- [ ] **Step 3: 验证语法正确**

Run: `cd mcp-servers/ha-server/src && python -c "from niu_ha_server import ha_scene; print('OK')"`

- [ ] **Step 4: 用真实 HA 测试创建**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import ha_setup, ha_scene, _read_config, _get_ha_client, _lookup_slug
import json, requests as req

ha_setup()
result = ha_scene(action='create', name='测试转换', config={'entities': {'light.yeelink_bslamp2_b1ce_light': {'state': 'on', 'brightness_pct': 50}}})
print('Create:', result)
if result.get('success'):
    config = _read_config()
    url, headers, _ = _get_ha_client(config)
    slug = _lookup_slug('scene', '测试转换')
    if slug:
        resp = req.get(f'{url}/api/config/scene/config/{slug}', headers=headers, timeout=10)
        cfg = resp.json()
        light_cfg = cfg.get('entities', {}).get('light.yeelink_bslamp2_b1ce_light', {})
        assert 'brightness' in light_cfg, f'ERROR: brightness not found: {list(light_cfg.keys())}'
        assert 'brightness_pct' not in light_cfg, 'ERROR: brightness_pct should have been converted'
        print('CONVERSION VERIFIED: brightness_pct->brightness, value:', light_cfg['brightness'])
    ha_scene(action='delete', name='测试转换', confirm=True)
"
`

- [ ] **Step 5: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): normalize scene entities in create/update"
```

---

### Task 5: 在 ha_scene activate 的 scene.apply 路径中调用 _normalize_scene_entities

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 在 scene.apply 路径中添加防御性转换**

在 `entities = scene_cfg.get("entities")` 之后、`scene.apply` 之前添加：
```python
            entities = _normalize_scene_entities(entities)
```

- [ ] **Step 2: 端到端测试**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import ha_setup, ha_scene, ha_status
import time

ha_setup()
status = ha_status(domain='light')
for d in status.get('devices', []):
    if 'yeelink_bslamp2_b1ce' in d.get('entity_id', ''):
        orig = d.get('properties', {}).get('brightness', '?')
        print(f'Original brightness: {orig}')
        break

ha_scene(action='create', name='测试激活', config={'entities': {'light.yeelink_bslamp2_b1ce_light': {'state': 'on', 'brightness_pct': 50}}})
result = ha_scene(action='activate', name='测试激活')
print(f'Activate: {result}')
assert result.get('success'), f'Failed: {result}'

time.sleep(1)
status2 = ha_status(domain='light')
for d in status2.get('devices', []):
    if 'yeelink_bslamp2_b1ce' in d.get('entity_id', ''):
        new_val = d.get('properties', {}).get('brightness', '?')
        print(f'New brightness: {new_val}')
        assert new_val != orig, f'Did not change!'
        print('ACTIVATION PASSED!')
        break

ha_scene(action='delete', name='测试激活', confirm=True)
"
`

- [ ] **Step 3: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): normalize scene entities in activate path"
```

---

### Task 6: 在 ha_scene get/list(detail) 中调用 _denormalize_scene_entities

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

这是出参方向的转换，确保 Agent 通过 get/list 看到的始终是服务参数名。

- [ ] **Step 1: 在 get 分支中，返回 config 前做反向转换**

当前 get 分支的成功路径代码为：
```python
return {"name": name, "entity_id": entity_id, "config": resp.json()}
```
需要先捕获 `resp.json()` 到变量，转换后再返回：
```python
        config = resp.json()
        if "entities" in config:
            config["entities"] = _denormalize_scene_entities(config["entities"])
        return {"name": name, "entity_id": entity_id, "config": config}
```

- [ ] **Step 2: 在 list(detail=true) 分支中，对每个场景的 config 做反向转换**

list 分支有**两条独立的代码路径**填充 `entry["config"]`，两条都需要做反向转换：

路径 A（states 中的场景）：当前代码为 `entry["config"] = resp.json()`，修改为：
```python
                    entry["config"] = resp.json()
                    if "entities" in entry["config"]:
                        entry["config"]["entities"] = _denormalize_scene_entities(entry["config"]["entities"])
```

路径 B（name_map 中的场景）：当前代码同样为 `entry["config"] = resp.json()`，修改为：
```python
                    entry["config"] = resp.json()
                    if "entities" in entry["config"]:
                        entry["config"]["entities"] = _denormalize_scene_entities(entry["config"]["entities"])
```

注意：两条路径的修改模式完全相同，在 `entry["config"] = resp.json()` 之后立即做反向转换。

- [ ] **Step 3: 验证**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import ha_setup, ha_scene
import json

ha_setup()
# 创建场景
ha_scene(action='create', name='测试get', config={'entities': {'light.yeelink_bslamp2_b1ce_light': {'state': 'on', 'brightness_pct': 50}}})

# get 返回的应该是服务参数名
result = ha_scene(action='get', name='测试get')
print('Get result:', json.dumps(result, ensure_ascii=False, indent=2))
if 'config' in result:
    entities = result['config'].get('entities', {})
    light_cfg = entities.get('light.yeelink_bslamp2_b1ce_light', {})
    assert 'brightness_pct' in light_cfg, f'ERROR: should return brightness_pct, got: {list(light_cfg.keys())}'
    assert 'brightness' not in light_cfg, f'ERROR: should not return brightness'
    print('GET VERIFIED: returns service param names (brightness_pct)')

ha_scene(action='delete', name='测试get', confirm=True)
"
`

- [ ] **Step 4: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): denormalize scene entities in get/list for consistent param names"
```

---

### Task 7: 修改 ha_scene snapshot 从 ATTR_WHITELIST 动态构建属性列表

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py` (ha_scene snapshot 分支)

- [ ] **Step 1: 替换硬编码属性名列表**

当前代码：
```python
                    for key in ("brightness", "color_temp_kelvin", "target_temperature", "hvac_mode", "percentage", "current_cover_position", "target_humidity", "preset_mode", "fan_mode"):
                        if key in attrs:
                            entities_config[eid][key] = attrs[key]
```

修改为：
```python
                    ent_domain = eid.split(".")[0]
                    for key in ATTR_WHITELIST.get(ent_domain, []):
                        if key in attrs:
                            entities_config[eid][key] = attrs[key]
```

变更说明：
- 不再使用 common_keys（审查指出设计缺陷）
- `color_temp_kelvin` 已在 Task 1 中加入 ATTR_WHITELIST["light"]
- `ent_domain` 从 entity_id 中提取
- ATTR_WHITELIST 中包含只读属性（如 `current_temperature`、`supported_features`），snapshot 提取它们后存入场景配置，scene.apply 会忽略不可写属性，无副作用

- [ ] **Step 2: 验证 snapshot**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import ha_setup, ha_scene
import json

ha_setup()
result = ha_scene(action='snapshot', name='测试快照', entity_ids=['light.yeelink_bslamp2_b1ce_light'])
print('Snapshot:', json.dumps(result, ensure_ascii=False))
if result.get('success'):
    ha_scene(action='delete', name='测试快照', confirm=True)
"
`

- [ ] **Step 3: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "fix(ha): snapshot uses ATTR_WHITELIST dynamically, remove hardcoded keys"
```

---

### Task 8: 更新 TOOL_SCHEMAS、YAML 和文档中 ha_scene 的描述

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py` (TOOL_SCHEMAS ha_scene description + config parameter description)
- Modify: `config/disk/ha-server.yaml` (ha_scene long description)
- Modify: `docs/manual-ha-setup.md` (第 815、822-825 行)

- [ ] **Step 1: 更新 TOOL_SCHEMAS ha_scene 顶层 description 和 config 参数 description**

顶层 description 更新为：
```
"管理场景：创建/查看/修改(update)/删除/激活/快照场景。场景是多设备瞬间切换到预设状态（如'阅读模式'、'晚安模式'）。有序列有延时用 ha_script，条件触发用 ha_automation。entities 参数名使用 ha_status services 字段中的服务参数名（如 brightness_pct），程序自动转换"
```

config 参数 description 更新为：
```
"场景配置 JSON。entities: 设备目标状态，键为 entity_id，值为目标状态字典。参数名使用 ha_status services 字段中的服务参数名（如 brightness_pct、position、tilt_position），程序自动转换为 HA 内部格式"
```

- [ ] **Step 2: 确认 YAML 描述**

确认 YAML 中 ha_scene 的 long 描述为：
```
"管理场景：创建/查看/修改/删除/激活/快照。场景是多设备瞬间切换到预设状态。有序列有延时用 ha_script，条件触发用 ha_automation。entities 参数名使用 ha_status services 字段中的服务参数名（如 brightness_pct），程序自动转换为 HA 内部格式"
```

- [ ] **Step 3: 更新 docs/manual-ha-setup.md**

第 815 行的创建示例，将 `"brightness": 200` 改为 `"brightness_pct": 78`（78% 约等于 200/255）。

第 822-825 行的 entities 属性名描述，更新为服务参数名：
```
**entities 参数名使用 ha_status services 字段中的服务参数名（程序自动转换）**：
- `light`：`brightness_pct` (0-100) / `color_temp_kelvin`
- `climate`：`temperature` / `hvac_mode`
- `switch` / `lock` / `cover`（`position`）/ `fan`（`percentage`）/ `humidifier`（`humidity`）
```

- [ ] **Step 4: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py config/disk/ha-server.yaml docs/manual-ha-setup.md
git commit -m "fix(ha): update ha_scene descriptions in TOOL_SCHEMAS/YAML/docs - use service param names"
```

---

### Task 9: 端到端集成测试

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 完整场景创建→激活→get→验证测试**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import ha_setup, ha_scene, ha_status, ha_control
import json, time

ha_setup()

# Test 1: brightness_pct 转换和激活
print('=== Test 1: brightness_pct conversion + activation ===')
ha_scene(action='create', name='e2e测试', config={'entities': {'light.yeelink_bslamp2_b1ce_light': {'state': 'on', 'brightness_pct': 50}}})
result = ha_scene(action='activate', name='e2e测试')
assert result.get('success'), f'Activate failed: {result}'
time.sleep(1)
status = ha_status(domain='light')
for d in status.get('devices', []):
    if 'yeelink_bslamp2_b1ce' in d.get('entity_id', ''):
        brightness = d.get('properties', {}).get('brightness')
        assert brightness is not None and brightness != 0, 'Brightness should have changed'
        print(f'PASS: brightness changed to {brightness}')
        break

# Test 2: get 返回服务参数名
print('=== Test 2: get returns service param names ===')
result = ha_scene(action='get', name='e2e测试')
entities = result.get('config', {}).get('entities', {})
light_cfg = entities.get('light.yeelink_bslamp2_b1ce_light', {})
assert 'brightness_pct' in light_cfg, f'Should have brightness_pct, got: {list(light_cfg.keys())}'
print(f'PASS: get returns brightness_pct={light_cfg[\"brightness_pct\"]}')

ha_scene(action='delete', name='e2e测试', confirm=True)

# Test 3: ha_control 仍正常
print('=== Test 3: ha_control works ===')
result = ha_control(entity_id='light.yeelink_bslamp2_b1ce_light', service='turn_on', service_data={'brightness_pct': 100})
assert result.get('success'), f'ha_control failed: {result}'
print('PASS: ha_control works')

print()
print('=== All e2e tests passed! ===')
"
`

- [ ] **Step 2: 验证 ha_automation 和 ha_script 不受影响**

Run: `cd mcp-servers/ha-server/src && python -c "
from niu_ha_server import ha_setup, ha_automation, ha_script, ha_subscribe
ha_setup()
assert 'automations' in ha_automation(action='list')
assert 'scripts' in ha_script(action='list')
print('All other tools work correctly')
"
`

---

## 自审检查

### 1. 数据流完整性

| 数据流 | 入参方向 | 出参方向 | 状态 |
|--------|---------|---------|------|
| Agent → ha_scene create | _normalize (服务→REST) | 无 | Task 4 |
| Agent → ha_scene update | _normalize (服务→REST) | 无 | Task 4 |
| Agent → ha_scene activate | _normalize (防御性) | 无 | Task 5 |
| ha_scene get → Agent | 无 | _denormalize (REST→服务) | Task 6 |
| ha_scene list → Agent | 无 | _denormalize (REST→服务) | Task 6 |
| ha_scene snapshot → HA | ATTR_WHITELIST 动态构建 | 无 | Task 7 |
| Agent → ha_control | 直接用服务参数名 | 无 | 不变 |
| Agent → ha_automation | 直接用服务参数名 | 无 | 不变 |
| Agent → ha_script | 直接用服务参数名 | 无 | 不变 |

### 2. ATTR_WHITELIST 一致性

- ATTR_WHITELIST 只使用 REST API 属性名（已通过 HA 源码验证）
- ha_status properties 提取、snapshot 属性提取、SVC_ATTR_MAP 目标名三者一致

### 3. 幂等性

- `_normalize_scene_entities` 检查 `attr_names` 集合，已是 REST API 属性名的不转换
- `_denormalize_scene_entities` 检查 `_ATTR_SVC_MAP`，不在映射中的不转换
- 双向转换是互逆的：normalize → denormalize = 原始值

### 4. 占位符扫描

无 TBD、TODO、implement later。
