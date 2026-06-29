# ha_status 精简/全量模式改造实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ha_status 从"默认全量返回"改为"默认精简列表 + entity_id 单设备全量"，解决 146 设备 46KB 输出被工具层截断到 30KB 导致后段设备（如扫地机器人）不可见的问题。

**Architecture:** ha_status 新增 `entity_id` 参数。当 `entity_id` 为空时（默认），返回所有设备的精简信息（name/area/entity_id/type/state/actions），跳过 services 和 properties 构建；当 `entity_id` 非空时，只返回该设备的完整信息（含 services 参数定义 + properties）。TOOL_SCHEMAS 和磁盘配置同步更新描述，引导 Agent 在调用带参数服务前先用 entity_id 查询参数详情。

**Tech Stack:** Python 3.11+, HA REST API, MCP 工具架构

---

## 现状分析

### 问题数据
- ha_status 默认返回 146 设备 × 平均 315 字符 ≈ 46KB
- 工具层截断阈值 `MAX_TOOL_RESULT_CHARS = 30000`（`agent/generic/agent_loop.py:173`）
- 截断后扫地机器人（在 HA states 顺序靠后）信息丢失，Agent "看不到"该设备

### 当前 ha_status 签名
```python
def ha_status(area: str = "", domain: str = "") -> dict:
```
- 无 entity_id 参数，无法查询单设备
- 默认返回所有设备的 services + properties（膨胀源）

### 设备 entry 结构（`__init__.py:913-956`）
- 基础字段（所有设备都有）：`name`/`area`/`entity_id`/`type`/`state`/`actions`
- 条件字段：`services`（带参数的服务定义）、`properties`（关键属性）
- **关键洞察**：services 和 properties 是条件性添加，跳过构建即可实现精简

### 截断机制
- `agent/generic/agent_loop.py:173-183` 的 `_truncate_tool_content`
- 3 处应用点（行 253, 480, 527）
- 通用机制，不针对 ha_status

### 其他工具依赖
- `ha_control`：从 ha_status 拿 `entity_id` 和 `actions`；自身通过 `services_cache` 验证服务。精简模式保留 actions 足够发起简单控制；带参数服务需先查单设备全量
- `ha_scene`/`ha_subscribe`/`ha_integrate`/`ha_automation`/`ha_script`：不依赖 ha_status 输出结构，不受影响

---

### Task 1: 新增 entity_id 参数，实现精简/全量模式切换

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

**原理**：ha_status 新增 `entity_id` 参数。当为空时返回精简列表（跳过 services/properties 构建）；非空时只返回该设备全量信息。

- [ ] **Step 1: 修改 ha_status 函数签名**

搜索 `def ha_status(area: str = "", domain: str = "") -> dict:` 定位（约第844行），改为：
```python
def ha_status(area: str = "", domain: str = "", entity_id: str = "") -> dict:
```

- [ ] **Step 2: 在过滤循环中添加 entity_id 过滤**

搜索 `if domain and ent_domain != domain:` 定位（约第898-899行），在其后添加 entity_id 过滤：
```python
        if domain and ent_domain != domain:
            continue
        if entity_id and eid != entity_id:
            continue
```

注意：entity_id 是精确匹配（完整 entity_id 字符串，如 `vacuum.18603118098`），不是模糊匹配。

- [ ] **Step 3: 修改 services 构建逻辑，精简模式跳过**

**重要**：只给现有的 services 构建块加 `if entity_id:` 外层守卫，**不修改块内任何语句**。守卫覆盖第 921-938 行（从 `# 添加有参数的服务定义` 注释到 `entry["services"] = entity_services`）。`entry["actions"]` 行（第 919 行）不在守卫范围内，保持原样。

当前代码（搜索 `# 添加有参数的服务定义` 定位，第 921-938 行）：
```python
        # 添加有参数的服务定义（用实体属性覆盖 domain 级别选项）
        entity_services = {}
        domain_svcs = services_cache.get(ent_domain, {})
        for act in entry["actions"]:
            if act in domain_svcs:
                fields = domain_svcs[act].get("fields", {})
                if fields:
                    svc_def = {"fields": {k: dict(v) for k, v in fields.items()}}
                    for fname, finfo in svc_def["fields"].items():
                        # 查找选项列表：先查映射表，再自动推断
                        opts_key = OPTIONS_ATTR_MAP.get((ent_domain, fname))
                        if not opts_key:
                            opts_key = _infer_opts_key(fname, attrs)
                        if opts_key and attrs.get(opts_key):
                            finfo["options"] = attrs[opts_key]
                    entity_services[act] = svc_def
        if entity_services:
            entry["services"] = entity_services
```

改为（在注释行后加 `if entity_id:`，整块缩进 +4 空格，块内逻辑完全不变）：
```python
        # 单设备全量模式：添加服务参数定义（精简模式跳过以减少输出体积）
        if entity_id:
            entity_services = {}
            domain_svcs = services_cache.get(ent_domain, {})
            for act in entry["actions"]:
                if act in domain_svcs:
                    fields = domain_svcs[act].get("fields", {})
                    if fields:
                        svc_def = {"fields": {k: dict(v) for k, v in fields.items()}}
                        for fname, finfo in svc_def["fields"].items():
                            # 查找选项列表：先查映射表，再自动推断
                            opts_key = OPTIONS_ATTR_MAP.get((ent_domain, fname))
                            if not opts_key:
                                opts_key = _infer_opts_key(fname, attrs)
                            if opts_key and attrs.get(opts_key):
                                finfo["options"] = attrs[opts_key]
                        entity_services[act] = svc_def
            if entity_services:
                entry["services"] = entity_services
```

- [ ] **Step 4: 修改 properties 构建逻辑，精简模式跳过**

同样只加 `if entity_id:` 外层守卫，不修改块内语句。守卫覆盖第 939-956 行（从 `# Extract useful properties` 注释到 `entry["properties"] = ...`）。

当前代码（搜索 `override_list = ATTR_OVERRIDES.get(ent_domain)` 定位，第 939-956 行）：
```python
        # Extract useful properties from attributes
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

改为（在注释行后加 `if entity_id:`，整块缩进 +4 空格）：
```python
        # 单设备全量模式：提取状态属性（精简模式跳过）
        if entity_id:
            # Extract useful properties from attributes
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

- [ ] **Step 5: 验证精简模式输出**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
import json
ha_setup()
status = ha_status()
out = json.dumps(status, ensure_ascii=False)
print('Total length:', len(out))
print('devices:', len(status.get('devices', [])))
# 检查精简模式不包含 services/properties
for d in status.get('devices', [])[:3]:
    print('device keys:', list(d.keys()))
    assert 'services' not in d, '精简模式不应包含 services'
    assert 'properties' not in d, '精简模式不应包含 properties'
print('OK: 精简模式无 services/properties')
"
```

Expected:
- Total length 远小于 30000（预计 8000-12000）
- 设备 keys 只有 `['name', 'area', 'entity_id', 'type', 'state', 'actions']`

- [ ] **Step 6: 验证单设备全量模式**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
import json
ha_setup()
# 找一个 climate 设备的 entity_id
status = ha_status()
climate_eid = None
for d in status['devices']:
    if d['entity_id'].startswith('climate.'):
        climate_eid = d['entity_id']
        break
print('Testing entity_id:', climate_eid)
# 查询单设备全量
detail = ha_status(entity_id=climate_eid)
print('devices count:', len(detail.get('devices', [])))
for d in detail.get('devices', []):
    print('device keys:', list(d.keys()))
    print('has services:', 'services' in d)
    print('has properties:', 'properties' in d)
    # climate 域有带参数服务（set_temperature 等），应有 services
    assert 'services' in d, 'climate 全量模式应包含 services'
    # climate 域在 ATTR_OVERRIDES 中有配置，应有 properties
    assert 'properties' in d, 'climate 全量模式应包含 properties'
print('OK: 全量模式包含 services/properties')
"
```

Expected: climate 单设备条目包含 services 和 properties 字段。

**注意**：并非所有域都有 services/properties。services 只在该设备有带参数服务时才构建；properties 只在该域有 ATTR_OVERRIDES 配置或 ATTR_COMMON 通用属性时才构建。climate 同时满足两者，故用于验证。

- [ ] **Step 7: 验证扫地机器人可见**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
import json
ha_setup()
status = ha_status()
out = json.dumps(status, ensure_ascii=False)
print('Total length:', len(out))
# 检查扫地机器人在精简列表中可见
found = False
for d in status['devices']:
    if 'vacuum' in d['entity_id'] or '扫地' in d.get('name',''):
        print('Found:', d['entity_id'], d['name'], 'state:', d['state'])
        found = True
assert found, '扫地机器人应可见'
assert len(out) < 30000, '精简模式输出应小于 30000 字符'
print('OK: 扫地机器人可见且输出未超阈值')
"
```

Expected: 扫地机器人可见，总输出 < 30000 字符。

- [ ] **Step 8: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): ha_status entity_id param - slim list by default, full detail on entity_id"
```

---

### Task 2: 更新 TOOL_SCHEMAS 工具描述

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

**原理**：更新 TOOL_SCHEMAS 中 ha_status 的 description 和 input_schema，告知 Agent 新的精简/全量行为，引导在调用带参数服务前先用 entity_id 查询参数详情。

- [ ] **Step 1: 更新 ha_status 的 TOOL_SCHEMAS**

搜索 `ha_status` 在 TOOL_SCHEMAS 中的定义（约第2144-2155行）。当前 description 和 input_schema：
```python
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
```

改为（更新 description 和 input_schema，保留 `name` 字段）：
```python
    "ha_status": {
        "name": "ha_status",
        "description": "查询智能家居设备、场景、自动化的当前状态。默认返回所有设备的精简列表（name/area/entity_id/type/state/actions），用于浏览全局设备。传入 entity_id 返回该设备的完整信息（含 services 服务参数定义和 properties 状态属性）。调用带参数的服务（如 set_temperature/set_fan_mode/select_option）前，先用 entity_id 查询获取参数名和可选值。可按 area 或 domain 过滤减少返回量。",
        "input_schema": {
            "type": "object",
            "properties": {
                "area": {"type": "string", "description": "按区域过滤（模糊匹配），如 '书房'"},
                "domain": {"type": "string", "description": "按设备类型过滤，如 'light'、'climate'"},
                "entity_id": {"type": "string", "description": "查询单个设备的完整信息（含服务参数定义）。传入时只返回该设备，含 services 和 properties 字段"},
            },
            "required": [],
        },
    },
```

**重要**：保留 `"name": "ha_status"` 字段——`tool_registry.py` 和 `runner.py` 依赖此字段注册工具，缺失会导致工具不可用。

- [ ] **Step 2: 验证 schema 加载**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import get_tool_schemas
import json
schemas = get_tool_schemas()
for s in schemas:
    if s.get('name') == 'ha_status':
        print('description:', s['description'][:100], '...')
        print('input_schema properties:', list(s['input_schema']['properties'].keys()))
        assert 'entity_id' in s['input_schema']['properties'], 'schema 应包含 entity_id'
        print('OK: schema 已更新')
        break
"
```

Expected: schema 包含 entity_id 参数。

- [ ] **Step 3: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "docs(ha): update ha_status TOOL_SCHEMAS with entity_id param and slim/full mode description"
```

---

### Task 3: 更新磁盘配置工具描述

**Files:**
- Modify: `config/disk/ha-server.yaml`

**原理**：磁盘配置（虚拟磁盘模式）的工具描述需要与 TOOL_SCHEMAS 保持一致。

- [ ] **Step 1: 读取当前磁盘配置**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot && sed -n '15,35p' config/disk/ha-server.yaml
```

查看 ha_status 当前的 short/long 描述和参数定义。

- [ ] **Step 2: 更新 ha_status 磁盘配置**

用 Edit 工具修改 `config/disk/ha-server.yaml` 中 ha_status 的描述和参数。当前配置在第 19-31 行：

```yaml
  - name: ha_status
    category: read
    short: "查询设备状态"
    long: "查询智能家居设备、场景、自动化的当前状态。返回按区域分类的设备列表，含可用操作及关键属性（温度、湿度等）"
    parameters:
      - name: area
        flag: area
        type: string
        description: "按区域过滤，如 书房"
      - name: domain
        flag: domain
        type: string
        description: "按设备类型过滤，如 light、climate"
```

改为（更新 short/long 描述，新增 entity_id 参数项，格式与现有 area/domain 列表项一致）：

```yaml
  - name: ha_status
    category: read
    short: "查询设备状态（默认精简，entity_id查全量）"
    long: "查询智能家居设备、场景、自动化的当前状态。默认返回所有设备的精简列表（name/area/entity_id/type/state/actions）。传入 entity_id 返回该设备完整信息（含 services 服务参数定义和 properties 状态属性）。调用带参数服务前先用 entity_id 查询获取参数详情。"
    parameters:
      - name: area
        flag: area
        type: string
        description: "按区域过滤，如 书房"
      - name: domain
        flag: domain
        type: string
        description: "按设备类型过滤，如 light、climate"
      - name: entity_id
        flag: entity_id
        type: string
        description: "查询单个设备的完整信息（含服务参数定义）。传入时只返回该设备"
```

- [ ] **Step 3: 验证 YAML 语法**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "import yaml; yaml.safe_load(open('config/disk/ha-server.yaml')); print('YAML OK')"
```

Expected: `YAML OK`

- [ ] **Step 4: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/disk/ha-server.yaml
git commit -m "docs(ha): update disk config ha_status description with entity_id param"
```

---

### Task 4: 端到端集成测试

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 验证精简模式不超阈值**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
import json
ha_setup()
status = ha_status()
out = json.dumps(status, ensure_ascii=False)
print('精简模式输出长度:', len(out))
assert len(out) < 30000, '精简模式应小于 30000 字符'
print('OK')
"
```

- [ ] **Step 2: 验证扫地机器人在精简列表中可见**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
import json
ha_setup()
status = ha_status()
found = [d for d in status['devices'] if 'vacuum' in d['entity_id']]
print('扫地机器人数:', len(found))
for d in found:
    print(' ', d['entity_id'], d['name'], 'state:', d['state'], 'actions:', d['actions'])
assert len(found) > 0, '扫地机器人应可见'
print('OK')
"
```

- [ ] **Step 3: 验证单设备全量包含 services**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
import json
ha_setup()
status = ha_status()
# 找扫地机器人
vacuum_eid = None
for d in status['devices']:
    if 'vacuum' in d['entity_id']:
        vacuum_eid = d['entity_id']
        break
print('查询单设备:', vacuum_eid)
detail = ha_status(entity_id=vacuum_eid)
for d in detail['devices']:
    print('keys:', list(d.keys()))
    print('services:', list(d.get('services', {}).keys()))
    print('properties keys:', list(d.get('properties', {}).keys()))
    assert 'services' in d, 'vacuum 应有 services（set_fan_speed 等带参数服务）'
print('OK: 全量模式正常')
"
```

- [ ] **Step 4: 验证 domain 过滤仍正常**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
import json
ha_setup()
status = ha_status(domain='climate')
print('climate 设备数:', len(status['devices']))
for d in status['devices'][:3]:
    print(' ', d['entity_id'], 'keys:', list(d.keys()))
    assert 'services' not in d, 'domain 过滤走精简模式'
print('OK: domain 过滤正常')
"
```

- [ ] **Step 5: 验证 ha_control 不受影响**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status, ha_control
import json
ha_setup()
status = ha_status()
# 找一个灯
light_eid = None
for d in status['devices']:
    if d['entity_id'].startswith('light.'):
        light_eid = d['entity_id']
        break
print('测试 ha_control:', light_eid)
result = ha_control(entity_id=light_eid, service='turn_on', service_data={})
print('result:', json.dumps(result, ensure_ascii=False, default=str)[:500])
print('OK: ha_control 正常')
"
```

- [ ] **Step 6: 验证 entity_id 不存在时返回空列表**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/ha-server/src && PYTHONPATH=src python3 -c "
from niu_ha_server import ha_setup, ha_status
ha_setup()
result = ha_status(entity_id='vacuum.nonexistent_xyz')
print('devices:', len(result.get('devices', [])))
assert len(result['devices']) == 0, '不存在的 entity_id 应返回空 devices'
print('OK: entity_id 不存在时返回空列表')
"
```

Expected: 返回 `connected: True` + 空 devices 列表。

---

## 自审检查

### 1. Spec 覆盖

- 默认精简模式（跳过 services/properties）→ Task 1 Step 3-4 ✅
- 单设备全量（entity_id 参数）→ Task 1 Step 1-2 ✅
- TOOL_SCHEMAS 更新 → Task 2 ✅
- 磁盘配置更新 → Task 3 ✅
- 端到端测试 → Task 4 ✅
- 精简模式保留 actions（够其他工具调用）→ Task 1 保留 actions ✅
- 截断阈值检查 → Task 4 Step 1 ✅

### 2. Placeholder 扫描

无 TBD/TODO。所有步骤包含具体代码和验证命令。

### 3. 类型一致性

- `entity_id: str = ""` 默认空字符串，与 area/domain 风格一致
- 精简模式 entry keys: `['name', 'area', 'entity_id', 'type', 'state', 'actions']`
- 全量模式 entry keys: 上述 + `services` + `properties`（条件性）
- TOOL_SCHEMAS 和磁盘配置的 entity_id 描述一致

### 4. 关键设计决策

- **entity_id 精确匹配**（不是模糊）：避免误匹配多个设备
- **entity_id 与 area/domain 可组合**：各条件为 AND 关系。entity_id 非空时实际只匹配1个设备，area/domain 此时无意义；若 entity_id 与不匹配的 domain/area 组合会返回空列表
- **精简模式保留 actions**：让 Agent 知道设备能做什么，可发起简单控制。`entry["actions"]` 行（第 919 行）不受 `if entity_id:` 守卫影响，始终构建
- **services/properties 用 `if entity_id:` 守卫**：只加外层 if 和缩进，不修改块内任何语句，最小改动
- **entity_id 非空时 scenes/automations 也被过滤**：因为 entity_id 过滤在循环顶部（`if entity_id and eid != entity_id: continue`），所有不匹配的实体（含 scenes/automations）都会被跳过。这是期望行为（查单设备时只关心该设备）
- **services/properties 是否构建取决于域配置**：services 只在该设备有带参数服务时才构建；properties 只在该域有 ATTR_OVERRIDES 配置或 ATTR_COMMON 通用属性时才构建。并非所有域全量模式都有这两个字段
