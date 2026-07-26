# HA-Server 通用化改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ha-server 从"白名单模式"改为"黑名单/动态模式"，使所有 HA 设备自动可见可控制，无需每遇到新设备类型就改代码。

**Architecture:** 核心改动是将 DOMAIN_MAP 从"过滤门"改为"可选覆盖配置"（DOMAIN_OVERRIDES），ha_status 过滤逻辑从"白名单包含"改为"黑名单排除"。ATTR_WHITELIST 从"正向白名单"改为"可选覆盖 + 通用属性提取"。snapshot _writable 从硬编码改为从 services_cache 动态推断。services 字段的8个 elif 分支改为映射表 + 自动推断规则。

**Tech Stack:** Python 3.11+, HA REST API (`/api/states`, `/api/services`)

---

## 硬编码审计结果

| 硬编码点 | 严重程度 | 改造方案 |
|---------|---------|---------|
| DOMAIN_MAP 过滤门 | 高 | 改为 DOMAIN_OVERRIDES，仅保留 type 标签 |
| ATTR_WHITELIST 零属性 | 高 | 改为 ATTR_OVERRIDES + 通用属性提取 |
| snapshot _writable | 高 | 从 services_cache fields 动态推断 |
| services 选项覆盖 8个elif | 中 | 改为 OPTIONS_ATTR_MAP + 自动推断 |
| EXCLUDED_DOMAINS | 中 | 改为可配置 |
| action 兼容模式 | 中 | 删除死代码，保留兼容映射 |
| supported_features 位掩码 | 中 | 提取为 SUPPORT_CHECKERS 字典 |
| _validate_entity_state 域判断 | 低 | 提取为 VALIDATION_RULES |
| ha_status 域分类 | 低 | 提取为 SPECIAL_CATEGORIES |
| SVC_ATTR_MAP | 低（HA设计限制） | 保持手动维护 |

---

### Task 1: DOMAIN_MAP 改为 DOMAIN_OVERRIDES，ha_status 过滤逻辑改为黑名单模式

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

> **注意**：计划中行号为初始文件行号，前面 Task 的修改会导致后续行号偏移。实现时应搜索代码内容定位，不要依赖行号。

**原理**：当前 ha_status 中 `info = DOMAIN_MAP.get(ent_domain); if not info: continue` 让不在白名单中的域完全不可见。改为：所有域都可见，DOMAIN_OVERRIDES 只提供中文 type 标签和默认 actions 的覆盖配置。

- [ ] **Step 1: 将 DOMAIN_MAP 重命名为 DOMAIN_OVERRIDES**

搜索 `DOMAIN_MAP = {`，改为 `DOMAIN_OVERRIDES = {`，内容不变。

- [ ] **Step 2: 更新 ha_status 中的过滤逻辑**

**重要**：源代码中 `EXCLUDED_DOMAINS` 检查已存在（`if ent_domain in EXCLUDED_DOMAINS: continue` 在 DOMAIN_MAP.get 之前），只需将 `DOMAIN_MAP` 改为 `DOMAIN_OVERRIDES`，并删除 `if not info: continue` 两行。不要重复添加 EXCLUDED_DOMAINS 检查。

当前代码（搜索 `DOMAIN_MAP.get(ent_domain)` 定位，注意它前面已有 `if ent_domain in EXCLUDED_DOMAINS: continue`）：
```python
        info = DOMAIN_MAP.get(ent_domain)
        if not info:
            continue
```

改为（只保留 info 赋值，删除 continue）：
```python
        info = DOMAIN_OVERRIDES.get(ent_domain)
```

- [ ] **Step 3: 更新 ha_status 中 type 字段的 fallback**

当前代码（搜索 `"type": info["type"] if info` 定位）：
```python
"type": info["type"] if info else ent_domain,
```

不需要改，但 `info` 现在可能为 None（不再被 continue 过滤），所以 fallback 到 `ent_domain` 会生效。对未知域，type 显示域名本身（如 "select"）。

- [ ] **Step 4: 更新 _get_entity_actions 中的引用**

当前代码（搜索 `DOMAIN_MAP.get(domain` 定位）：
```python
info = DOMAIN_MAP.get(domain, {})
```

改为：
```python
info = DOMAIN_OVERRIDES.get(domain, {})
```

同时在 fallback 路径中添加警告：当 `domain_services` 为空且域不在 DOMAIN_OVERRIDES 中时，输出日志提示 services_cache 可能需要刷新：
```python
if not domain_services:
    info = DOMAIN_OVERRIDES.get(domain, {})
    if not info:
        print(f"[HA] warning: no services_cache for domain '{domain}', run ha_setup to refresh")
```

- [ ] **Step 5: 验证语法和功能**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status; import json; ha_setup(); status = ha_status(domain='select'); print(json.dumps(status, ensure_ascii=False, indent=2))"`

Expected: 能看到 select 域的设备（如微波炉工作模式选择器）

- [ ] **Step 6: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "refactor(ha): DOMAIN_MAP → DOMAIN_OVERRIDES, blacklist mode for device visibility"
```

---

### Task 2: ATTR_WHITELIST 改为 ATTR_OVERRIDES + 通用属性提取

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

**原理**：当前 ATTR_WHITELIST 是正向白名单，不在列表中的域不提取任何属性。改为：有覆盖配置的域只提取列出的属性（保持现有行为），无覆盖配置的域提取通用属性。

- [ ] **Step 1: 定义通用属性提取规则**

在 ATTR_WHITELIST 定义之前，添加：
```python
# 通用属性：对未知域提取这些属性（如果存在）
# 注意：不含 "option"（当前选中值已作为 state 返回，冗余）
ATTR_COMMON = {"options", "min", "max", "step", "mode", "available_modes",
               "unit_of_measurement", "device_class", "state_class"}
```

- [ ] **Step 2: 将 ATTR_WHITELIST 重命名为 ATTR_OVERRIDES**

搜索 `ATTR_WHITELIST = {`，改为 `ATTR_OVERRIDES = {`，内容不变。

- [ ] **Step 3: 更新 ha_status 中 properties 构建逻辑**

当前代码（搜索 `ATTR_WHITELIST.get(ent_domain` 定位）：
```python
whitelist = ATTR_WHITELIST.get(ent_domain, [])
if whitelist:
    props = {}
    for attr_name in whitelist:
        val = attrs.get(attr_name)
        if val is not None:
            props[attr_name] = val
    if props:
        entry["properties"] = _convert_attrs_to_svc_names(props, ent_domain)
```

改为：
```python
override_list = ATTR_OVERRIDES.get(ent_domain)
if override_list is not None:
    # 有显式覆盖配置的域：只提取列出的属性
    props = {}
    for attr_name in override_list:
        val = attrs.get(attr_name)
        if val is not None:
            props[attr_name] = val
else:
    # 未知域：提取通用属性
    props = {}
    for attr_name in ATTR_COMMON:
        val = attrs.get(attr_name)
        if val is not None:
            props[attr_name] = val
if props:
    entry["properties"] = _convert_attrs_to_svc_names(props, ent_domain)
```

关键区别：
- `ATTR_OVERRIDES.get(ent_domain)` 返回 `None` 表示"无覆盖配置"，走通用路径
- `ATTR_OVERRIDES.get(ent_domain)` 返回 `[]`（空列表）表示"显式无属性"（如 switch、lock），不走通用路径
- 通用路径只提取 ATTR_COMMON 中列出的属性，避免返回过多内部属性

- [ ] **Step 4: 更新 snapshot 中 _writable 的引用**

当前 snapshot 的 _writable 字典是独立的硬编码，此 Task 不修改它（Task 3 处理）。但 ATTR_WHITELIST 重命名后，需要搜索所有 `ATTR_WHITELIST` 引用并更新为 `ATTR_OVERRIDES`。

- [ ] **Step 5: 验证 select 域的属性提取**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status; import json; ha_setup(); status = ha_status(domain='select'); print(json.dumps(status, ensure_ascii=False, indent=2))"`

Expected: select 设备的 properties 中包含 `options` 属性（不含 `option`，因当前选中值已作为 state 返回）

- [ ] **Step 6: 验证现有域不受影响**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status; import json; ha_setup(); status = ha_status(domain='light'); for d in status.get('devices', []): print(d.get('entity_id'), d.get('properties', {})); break"`

Expected: light 域的 properties 仍然只包含白名单中的属性（brightness_pct, color_mode, supported_color_modes, color_temp_kelvin）

- [ ] **Step 7: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "refactor(ha): ATTR_WHITELIST → ATTR_OVERRIDES + ATTR_COMMON for universal attribute extraction"
```

---

### Task 3: snapshot _writable 改为从 services_cache 动态推断

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

**原理**：当前 snapshot 的 _writable 字典硬编码了每个域的可写属性。改为从 services_cache 的 fields 定义动态推断：对每个域，收集所有服务的 fields（排除 entity_id），这些就是可写属性名（服务参数名），通过 _normalize_scene_entities 转换为 REST API 属性名。

- [ ] **Step 1: 实现动态可写属性推断函数**

在 `_normalize_scene_entities` 函数之后添加：
```python
def _get_writable_attrs(domain: str, services_cache: dict = None) -> tuple:
    """从 services_cache 推断指定域的可写属性名（服务参数名）。

    返回的服务参数名需要在 snapshot 中通过 _svc_to_attr 映射为 REST API 属性名。
    当 services_cache 不可用时返回空 tuple（调用方应记录警告）。
    """
    if services_cache is None:
        services_cache = _read_services_cache()
    domain_services = services_cache.get(domain, {})
    if not domain_services:
        return ()
    # 排除动作性服务的非状态参数（这些不是可快照的属性）
    _ACTION_ONLY_FIELDS = {"command", "params", "message", "text", "url"}
    writable = set()
    for svc_name, svc_info in domain_services.items():
        fields = svc_info.get("fields", {})
        for fname, finfo in fields.items():
            if fname != "entity_id" and not fname.startswith("additional_") and fname not in _ACTION_ONLY_FIELDS:
                writable.add(fname)
    return tuple(sorted(writable))
```

- [ ] **Step 2: 替换 snapshot 中的 _writable 硬编码字典**

**重要**：替换范围只包括 `_writable = {...}` 字典定义和 `for key in _writable.get(ent_domain, ()):` 循环体，**不要**引入新的 `for eid in entity_ids:` 循环（现有循环已存在，需保留）。`_services_cache = _read_services_cache()` 应放在现有 `entities_config = {}` 之后、现有 `for eid in entity_ids:` 之前。

当前代码（搜索 `_writable = {` 定位，包含 climate/light/fan/cover/humidifier/vacuum/media_player 的字典，以及紧随其后的 `for key in _writable.get(ent_domain, ()):` 循环）：
```python
                    _writable = {
                        "climate": ("temperature", "target_temp_high", "target_temp_low", "hvac_mode", "preset_mode", "fan_mode", "swing_mode", "swing_horizontal_mode"),
                        "light": ("brightness", "color_mode", "color_temp_kelvin", "effect"),
                        "fan": ("percentage", "direction", "preset_mode"),
                        "cover": ("current_position", "current_tilt_position"),
                        "humidifier": ("humidity", "mode"),
                        "vacuum": ("fan_speed",),
                        "media_player": ("source", "volume_level"),
                    }
                    for key in _writable.get(ent_domain, ()):
                        # ... 现有循环体（从 attrs 提取 key 写入 entities_config[eid]）...
```

**第一步**：在 `entities_config = {}` 之后、`for eid in entity_ids:` 之前插入一行（搜索 `entities_config = {}` 定位）：
```python
                    _services_cache = _read_services_cache()
```

**第二步**：将 `_writable = {...}` 字典和 `for key in _writable.get(ent_domain, ()):` 循环替换为（缩进与现有循环体一致）：

```python
                    writable = _get_writable_attrs(ent_domain, _services_cache)
                    if not writable and ent_domain not in EXCLUDED_DOMAINS:
                        print(f"[HA] warning: no writable attrs for {ent_domain}, services_cache may be empty")
                    # 将服务参数名映射为 REST API 属性名，用于从 attrs 中查找
                    # 重要假设：不在 SVC_ATTR_MAP 中的服务参数名，假设与 REST API 属性名相同
                    # 如果 HA 未来引入新的名称差异，必须同步更新 SVC_ATTR_MAP
                    _svc_to_attr = {}
                    for param_name in writable:
                        mapping = SVC_ATTR_MAP.get((ent_domain, param_name))
                        if mapping:
                            _svc_to_attr[mapping[0]] = param_name  # rest_attr -> param_name
                        else:
                            _svc_to_attr[param_name] = param_name  # 名字相同
                    for attr_key in _svc_to_attr:
                        if attr_key in attrs:
                            entities_config[eid][attr_key] = attrs[attr_key]
```

这样 snapshot 提取的属性键是 REST API 名（与 /api/states 返回一致），存入 scene config 也是 REST API 名。后续 get/list 通过 _denormalize 转回服务参数名。

**已知行为变化**：动态推断会捕获比原硬编码更多的可写属性。例如 `climate` 域现在会捕获 `humidity`（来自 `climate.set_humidity` 服务，是目标湿度，可写）。这是正确的改进——原硬编码遗漏了 `humidity`。只读属性（如 `current_humidity`、`current_temperature`）不在任何服务的 fields 中，不会被捕获。

- [ ] **Step 3: 验证 snapshot 功能**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_scene; import json; ha_setup(); result = ha_scene(action='snapshot', name='测试动态', entity_ids=['light.yeelink_bslamp2_b1ce_light']); print(json.dumps(result, ensure_ascii=False)); ha_scene(action='delete', name='测试动态', confirm=True) if result.get('success') else None"`

- [ ] **Step 4: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "refactor(ha): snapshot _writable from services_cache dynamic inference"
```

---

### Task 4: services 选项覆盖从 8个elif 改为映射表 + 自动推断

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

**原理**：当前 ha_status 中有 8 个硬编码的 elif 分支，将服务参数名映射到 attributes 中的选项列表键名。改为映射表 + 自动推断规则。

- [ ] **Step 1: 定义 OPTIONS_ATTR_MAP 映射表**

在 EXCLUDED_DOMAINS 之后添加：
```python
# 服务参数 → 状态属性中的选项列表键名映射
# 自动推断规则：先查此映射，未命中则尝试 {fname}_list 和 {fname}s
OPTIONS_ATTR_MAP = {
    ("vacuum", "fan_speed"): "fan_speed_list",
    ("climate", "hvac_mode"): "hvac_modes",
    ("climate", "preset_mode"): "preset_modes",
    ("climate", "fan_mode"): "fan_modes",
    ("climate", "swing_mode"): "swing_modes",
    ("humidifier", "mode"): "available_modes",
    ("fan", "preset_mode"): "preset_modes",
    ("media_player", "source"): "source_list",
    ("select", "option"): "options",
}
```

- [ ] **Step 2: 替换 8个 elif 分支为通用逻辑**

当前代码（搜索 `ent_domain == "vacuum" and fname == "fan_speed"` 定位，后面有7个类似elif）：
```python
if ent_domain == "vacuum" and fname == "fan_speed" and attrs.get("fan_speed_list"):
    finfo["options"] = attrs["fan_speed_list"]
elif ent_domain == "climate" and fname == "hvac_mode" and attrs.get("hvac_modes"):
    ...
```

替换为：
```python
# 查找选项列表：先查映射表，再自动推断
opts_key = OPTIONS_ATTR_MAP.get((ent_domain, fname))
if not opts_key:
    # 自动推断：尝试 {fname}_list 和 {fname}s
    opts_key = f"{fname}_list" if attrs.get(f"{fname}_list") else (f"{fname}s" if attrs.get(f"{fname}s") else None)
if opts_key and attrs.get(opts_key):
    finfo["options"] = attrs[opts_key]
```

- [ ] **Step 3: 验证功能**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status; import json; ha_setup(); status = ha_status(domain='climate'); print(json.dumps(status, ensure_ascii=False, indent=2))"`

Expected: climate 设备的 services 中 hvac_mode 的 fields 仍包含 options 列表

- [ ] **Step 4: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "refactor(ha): services options from elif chain to OPTIONS_ATTR_MAP + auto-inference"
```

---

### Task 5: EXCLUDED_DOMAINS 扩展 + 可配置化

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

**原理**：当前 EXCLUDED_DOMAINS 只有 9 个域，缺少一些系统/内部域。扩展默认列表，并允许用户通过配置文件自定义。

- [ ] **Step 1: 扩展 EXCLUDED_DOMAINS**

当前代码（搜索 `EXCLUDED_DOMAINS = {` 定位）：
```python
EXCLUDED_DOMAINS = {
    "input_boolean", "input_number", "input_select", "input_button",
    "sun", "zone", "person", "update", "weather",
}
```

改为：
```python
EXCLUDED_DOMAINS = {
    # HA 辅助元素
    "input_boolean", "input_number", "input_select", "input_button",
    # 系统域
    "sun", "zone", "person", "update", "weather",
    "persistent_notification", "tag",
    "cloud", "system_health", "hassio",
    # HA 内部实体
    "conversation", "homeassistant", "stt", "tts", "wake_word",
}
```

注意：`timer` 和 `counter` 未加入排除列表——用户可能期望 Agent 能查询和管理倒计时/计数器（如"还有多久 timer 到期"）。如果实际使用中发现这两个域造成噪声，可在后续迭代中加入。

- [ ] **Step 2: 验证新增排除域不影响现有设备**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status; ha_setup(); status = ha_status(); print('devices:', len(status.get('devices', []))); print('scenes:', len(status.get('scenes', []))); print('automations:', len(status.get('automations', [])))"`

- [ ] **Step 3: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "refactor(ha): extend EXCLUDED_DOMAINS with system/internal domains"
```

---

### Task 6: 删除死代码 ACTION_SERVICE_MAP，清理兼容模式

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

**原理**：ACTION_SERVICE_MAP 定义了但从未被使用，是死代码。删除它减少混淆。

- [ ] **Step 1: 删除 ACTION_SERVICE_MAP**

搜索 `ACTION_SERVICE_MAP` 定义，删除整个字典定义。

- [ ] **Step 2: 确认无引用**

搜索文件中所有 `ACTION_SERVICE_MAP` 引用，确认删除后无报错。

- [ ] **Step 3: 验证语法**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_control; print('OK')"`

- [ ] **Step 4: 临时提交**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "chore(ha): remove dead code ACTION_SERVICE_MAP"
```

---

### Task 7: 端到端集成测试

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 验证 select 域设备可见**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status; import json; ha_setup(); status = ha_status(domain='select'); print(json.dumps(status, ensure_ascii=False, indent=2))"`

- [ ] **Step 2: 验证 ha_control 能控制 select 域**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status, ha_control; import json; ha_setup(); status = ha_status(domain='select'); for d in status.get('devices', []): print(d.get('entity_id'), d.get('properties', {})); if d.get('properties', {}).get('options'): first_opt = d['properties']['options'][0]; result = ha_control(entity_id=d['entity_id'], service='select_option', service_data={'option': first_opt}); print('Control:', result.get('success')); break"`

- [ ] **Step 3: 验证 light 域不受影响**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_status, ha_scene; import json; ha_setup(); status = ha_status(domain='light'); for d in status.get('devices', []): print(d.get('entity_id'), d.get('properties', {})); break"`

- [ ] **Step 4: 验证场景流程仍正常**

Run: `cd <repo_root>/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "from niu_ha_server import ha_setup, ha_scene; import json; ha_setup(); ha_scene(action='create', name='e2e通用', config={'entities': {'light.yeelink_bslamp2_b1ce_light': {'state': 'on', 'brightness_pct': 50}}}); result = ha_scene(action='activate', name='e2e通用'); print('Activate:', result.get('success')); result = ha_scene(action='get', name='e2e通用'); entities = result.get('config', {}).get('entities', {}); light_cfg = entities.get('light.yeelink_bslamp2_b1ce_light', {}); print('Get brightness_pct:', light_cfg.get('brightness_pct')); ha_scene(action='delete', name='e2e通用', confirm=True)"`

---

## 自审检查

### 1. Spec 覆盖

- DOMAIN_MAP 硬编码 → Task 1 ✅
- ATTR_WHITELIST 硬编码 → Task 2 ✅
- snapshot _writable 硬编码 → Task 3 ✅
- services 选项 elif 硬编码 → Task 4 ✅
- EXCLUDED_DOMAINS 不完整 → Task 5 ✅
- ACTION_SERVICE_MAP 死代码 → Task 6 ✅
- 端到端测试 → Task 7 ✅
- supported_features 位掩码 → 未包含（影响较小，后续迭代）
- _validate_entity_state 域判断 → 未包含（影响较小，后续迭代）
- SVC_ATTR_MAP → 不需要改（HA 设计限制）

### 2. Placeholder 扫描

无 TBD/TODO/placeholder。所有步骤包含具体代码和验证命令。

### 3. 类型一致性

- DOMAIN_OVERRIDES 与原 DOMAIN_MAP 结构相同（dict[str, dict]）
- ATTR_OVERRIDES 与原 ATTR_WHITELIST 结构相同（dict[str, list]）
- _get_writable_attrs 返回 tuple[str]，与原 _writable 字典的 value 类型一致
- OPTIONS_ATTR_MAP 使用 (domain, fname) 元组作为 key，与 SVC_ATTR_MAP 模式一致
