# 智能家居集成设计规范

> 日期: 2026-06-22
> 状态: 设计完成
> 核心命题: 让 AI Agent 成为智能家居的操控者，而非底层 API 对接者。用户说"把书房灯关了"，Agent 就能关；用户说"温度超过 25 度提醒我"，Agent 就能监听。

---

## 1. 架构概览

三个组件，职责清晰：

| 组件 | 位置 | 职责 |
|------|------|------|
| **ha-server** | `mcp-servers/ha-server/` | MCP Server，5 个工具，Agent 的唯一入口 |
| **HAWatcher** | `niu_api/internal/ha_watcher/` | 守护线程，条件触发推送，与 scheduler-server 同模式 |
| **ha-config.json** | `~/.niu/` | 配置存储（HA 连接信息 + 触发器列表） |

数据流：

```
用户 → Agent → ha-server 工具 → HA REST API
                        ↓
              ha_subscribe 写入 ha-config.json
                        ↓
              HAWatcher 读取 → WebSocket subscribe_trigger
                        ↓
              触发 → trigger_callback → ChatQueue → 推送给用户
```

---

## 2. 工具设计

### 2.1 ha_status — 一次查询，全部呈现

**可选参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| area | 否 | 按区域过滤，如 "书房" |
| domain | 否 | 按设备类型过滤，如 "light"、"climate" |

不传参数时返回所有设备、场景、自动化、区域信息。传 `area` 或 `domain` 时只返回匹配项，减少上下文占用。

**前置检查：** 如果 `~/.niu/ha-config.json` 不存在或无法连接 HA，返回：

```json
{"connected": false, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}
```

**实现：** 并发调用三个 API：
1. `GET /api/states` — 所有实体状态
2. WebSocket `config/device_registry/list` — 设备注册信息（含 area_id）
3. WebSocket `config/area_registry/list` — 区域列表

然后按 domain 分类，合并设备名和区域信息：

```json
{
  "connected": true,
  "areas": [
    {"id": "study", "name": "书房"},
    {"id": "bedroom", "name": "主卧"}
  ],
  "devices": [
    {
      "name": "书房灯",
      "area": "书房",
      "entity_id": "light.yeelink_bslamp2_b1ce_light",
      "type": "灯",
      "state": "on",
      "actions": ["turn_on", "turn_off", "toggle", "set_brightness"]
    }
  ],
  "scenes": [
    {
      "name": "阅读模式",
      "entity_id": "scene.reading_mode",
      "state": "scening",
      "actions": ["activate"]
    }
  ],
  "automations": [
    {
      "name": "离家关灯",
      "entity_id": "automation.leave_home_off_lights",
      "state": "on",
      "actions": ["trigger", "turn_on", "turn_off"]
    }
  ]
}
```

**domain → type 映射：**

| domain | type | actions |
|--------|------|---------|
| light | 灯 | turn_on, turn_off, toggle, set_brightness |
| climate | 空调/温控 | turn_on, turn_off, set_temperature |
| sensor | 传感器 | (只读，无 actions) |
| switch | 开关 | turn_on, turn_off, toggle |
| fan | 风扇 | turn_on, turn_off, toggle |
| cover | 窗帘 | open, close, toggle |
| lock | 门锁 | lock, unlock |
| humidifier | 加湿器 | turn_on, turn_off |
| vacuum | 扫地机 | turn_on, turn_off |
| media_player | 媒体 | turn_on, turn_off, toggle |
| camera | 摄像头 | (只读) |
| scene | 场景 | activate |
| script | 脚本 | run |
| automation | 自动化 | trigger, turn_on, turn_off |

**过滤规则：** 排除 domain 为 `input_boolean`, `input_number`, `input_select`, `input_button`, `sun`, `zone`, `person`, `update`, `weather` 的辅助/系统实体。

### 2.2 ha_control — 统一控制入口

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| entity_id | 是 | 实体 ID，如 `light.xxx` |
| action | 是 | 动作名 |
| value | 否 | 动作参数（亮度 0-100、温度等） |

**action → service 映射：**

| action | service | value 用途 |
|--------|---------|------------|
| turn_on | {domain}/turn_on | — |
| turn_off | {domain}/turn_off | — |
| toggle | {domain}/toggle | — |
| activate | scene/turn_on | — |
| run | script/turn_on | — |
| trigger | automation/trigger | — |
| set_brightness | light/turn_on | brightness = value × 2.55（value 范围 0-100，HA brightness 范围 0-255） |
| set_temperature | climate/set_temperature | temperature = value |
| open | cover/open_cover | — |
| close | cover/close_cover | — |
| lock | lock/lock | — |
| unlock | lock/unlock | — |

**返回：**

```json
// 成功
{"success": true, "entity_id": "light.xxx", "state": "on", "attributes": {"brightness": 128}}

// 失败
{"success": false, "error": "实体不存在或服务调用失败: ..."}
```

**实现：** 从 entity_id 提取 domain，先校验 action 是否在该 domain 允许的 actions 列表中（参照上方映射表），不合法则返回 `{"success": false, "error": "动作 '{action}' 不适用于 {type} 设备，可用动作: {actions}"}`。校验通过后映射到 service，调用 `POST /api/services/{domain}/{service}`。HA 的服务调用返回变更实体数组，需从中找到目标 entity_id 提取 state 和 attributes；如目标不在数组中，回退 `GET /api/states/{entity_id}` 确认新状态。set_brightness 额外传 `brightness` 参数（value × 2.55 取整）。set_temperature 额外传 `temperature` 参数。

### 2.3 ha_subscribe — 条件监听与推送

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| entity_id | 否 | 监听的实体 ID（新增订阅时必填） |
| condition | 否 | 条件类型：state_change / above / below（新增订阅时必填） |
| value | 否 | above/below 时必填，阈值 |
| from_state | 否 | state_change 时的起始状态过滤，如 "off" |
| to_state | 否 | state_change 时的目标状态过滤，如 "on" |
| description | 否 | 触发时的描述文本，默认 "{entity_id} {condition} {value}" |
| trigger_id | 否 | 触发器唯一标识；不传时由工具自动生成（推荐）；取消订阅时必填 |
| operation | 否 | "unsubscribe" 表示取消（需配合 trigger_id），"list" 表示查询当前订阅列表，不传表示新增 |

**新增订阅流程：**
1. 生成唯一 trigger_id（格式 `ha_trig_{timestamp}_{random}`，确保不与已有 ID 重复）
2. 原子写入 `~/.niu/ha-config.json` 的 triggers 列表（先写临时文件 + `os.rename()`）
3. 通过 `threading.Event` 通知 HAWatcher 立即重读配置，消除 5 秒轮询延迟
4. HAWatcher 创建 subscribe_trigger 连接
5. 触发时通过 `trigger_callback` → ChatQueue 推送：`[智能家居] {description}`

**取消订阅流程：**
1. 从 `~/.niu/ha-config.json` 的 triggers 列表移除（原子写入）
2. 通过 `threading.Event` 通知 HAWatcher 立即重读配置
3. HAWatcher 取消对应订阅

**condition → trigger 映射：**

| condition | trigger 配置 |
|-----------|-------------|
| state_change | `{"platform": "state", "entity_id": "...", "from": from_state, "to": to_state}` — from/to 为可选过滤，不传则任意状态变化都触发 |
| above | `{"platform": "numeric_state", "entity_id": "...", "above": value}` |
| below | `{"platform": "numeric_state", "entity_id": "...", "below": value}` |

**注意：** 不带 from/to 的 state_change 会触发该实体的任何属性变化（包括亮度、颜色等），对于高频传感器建议使用 above/below 条件。

**返回：**

```json
// 新增成功
{"success": true, "trigger_id": "ha_trig_1719014400_a3f2", "message": "已订阅: 温度超过25度"}

// 取消成功
{"success": true, "trigger_id": "ha_trig_1719014400_a3f2", "message": "已取消订阅"}

// 查询订阅列表
// threshold 字段仅在 condition 为 above/below 时存在，state_change 条件无此字段
{
  "triggers": [
    {"id": "ha_trig_1719014400_a3f2", "entity_id": "sensor.xxx_temperature", "condition": "above", "threshold": 25, "description": "温度超过25度"},
    {"id": "ha_trig_1719014400_b7e1", "entity_id": "light.xxx", "condition": "state_change", "from_state": "on", "to_state": "off", "description": "灯关了"}
  ]
}

// 失败
{"success": false, "error": "..."}
```

### 2.4 ha_setup — 连接配置

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| ha_url | 否 | HA 地址，如 `http://localhost:8123` |
| ha_token | 否 | Long-Lived Access Token |

**行为：**
- **有参数时：** 验证连接（`GET /api/`），成功后原子写入 `~/.niu/ha-config.json`（文件权限 600），启动/重启 HAWatcher
- **无参数时：** 读取 `~/.niu/ha-config.json`，验证 HA 连接是否存活（`GET /api/`），返回配置状态 + 连接状态 + 当前 triggers 列表

**返回：**

```json
// 配置成功
{"connected": true, "ha_url": "http://localhost:8123", "version": "2026.6.0"}

// 未配置
{"connected": false, "error": "未配置 Home Assistant"}

// 连接失败
{"connected": false, "error": "无法连接到 Home Assistant: ..."}

// 无参数查询（连接正常）
{
  "connected": true,
  "ha_url": "http://localhost:8123",
  "version": "2026.6.0",
  "triggers": [
    {"id": "ha_trig_001", "entity_id": "sensor.xxx_temperature", "condition": "above", "threshold": 25, "description": "温度超过25度"}
  ]
}
```

### 2.5 ha_integrate — 集成配置流

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| handler | 是 | 集成域名，如 `xiaomi_miot` |
| flow_id | 否 | 配置流 ID（后续步骤必填） |
| data | 否 | 表单数据，类型 object，键值对如 `{"username": "xxx", "password": "yyy"}`（后续步骤必填） |
| operation | 否 | "delete" 表示删除集成 |
| entry_id | 否 | 删除集成时必填 |

**多步配置流流程：**

1. **发起：** `ha_integrate(handler="xiaomi_miot")` → 调用 `POST /api/config/config_entries/flow` → 返回第一步表单字段
2. **推进：** `ha_integrate(handler="xiaomi_miot", flow_id="xxx", data={"username": "...", "password": "..."})` → 调用 `POST /api/config/config_entries/flow/{flow_id}` → 返回下一步或 create_entry
3. **重复步骤 2** 直到返回 `create_entry`
4. **删除：** `ha_integrate(operation="delete", entry_id="xxx")` → 调用 `DELETE /api/config/config_entries/entry/{entry_id}`

**返回格式：**

```json
// 表单步骤（fields 从 HA data_schema 转换而来）
{
  "type": "form",
  "flow_id": "abc123",
  "step_id": "user",
  "title": "Xiaomi Miot Auto",
  "fields": [
    {"name": "username", "type": "string", "required": true, "label": "小米账号"},
    {"name": "password", "type": "string", "required": true, "label": "密码"},
    {"name": "server", "type": "select", "options": ["cn", "i2", "sg", "de"], "default": "cn", "label": "服务器"}
  ],
  "description": "请输入小米账号信息"
}

// 注：HA 返回的 data_schema 是 Voluptuous 序列化格式，
// 实现时需要转换为上述简化的 fields 列表，提取 name/type/required/default/label

// 配置完成
{
  "type": "create_entry",
  "title": "Xiaomi Miot Auto",
  "entry_id": "def456",
  "message": "集成配置成功，已发现 6 个设备"
}

// 需要外部验证（如小米短信验证）
{
  "type": "form",
  "flow_id": "abc123",
  "step_id": "need_verify",
  "fields": [],
  "description": "请在浏览器中打开以下链接完成验证: https://..."
}

// 删除成功
{"success": true, "message": "集成已删除"}
```

**Config Flow API（HA 2026.6，实施前必须验证）：**
- 发起：`POST /api/config/config_entries/flow`，body `{"handler": "xiaomi_miot", "show_options": false}`
- 推进：`POST /api/config/config_entries/flow/{flow_id}`，body 为表单字段键值对（data 参数直接展开）。注意：body 格式尚未实际验证，可能是 `{"user_input": {...data}}` 或直接展开 data 字段，实施时必须用真实 HA 实例测试确认
- 删除：`DELETE /api/config/config_entries/entry/{entry_id}`

**注意：** HA 2026.6 中 Config Flow 的 WebSocket API（`config/integration/initialize` / `config/integration/step`）已废弃，必须使用 REST API。实施前需再次验证 REST 端点可用性，如不可用则回退到 WebSocket 方案。

---

## 3. 配置文件

**路径：** `~/.niu/ha-config.json`

**安全：** 文件权限 600（仅 owner 可读写），ha_setup 创建时设置。ha_token 不得出现在工具返回值或日志输出中，实现时必须在写入 stderr/log 前脱敏。LLM 工具调用参数中的 token 在对话历史中可见，此为已知限制。

**原子写入：** 所有写入操作使用"写临时文件 + os.rename()"模式，防止 HAWatcher 读到半写状态。写入后通过 `threading.Event` 通知 HAWatcher 立即重读。

**写入锁：** 模块级 `threading.Lock`（与 memory-server 的 `_memory_file_lock` 同模式），序列化所有 read-modify-write 操作：acquire → 读文件 → 修改内存 → 原子写入 → release → set Event。

**字段映射：** 工具参数名 `value` 在写入配置文件时映射为 `threshold`（更明确的持久化语义），读取时反向映射。

```json
{
  "ha_url": "http://localhost:8123",
  "ha_token": "eyJhbGciOi...",
  "triggers": [
    {
      "id": "ha_trig_1719014400_a3f2",
      "entity_id": "sensor.miaomiaoce_t1_4d15_temperature",
      "condition": "above",
      "threshold": 25,
      "description": "温度超过25度"
    },
    {
      "id": "ha_trig_1719014400_b7e1",
      "entity_id": "light.yeelink_bslamp2_b1ce_light",
      "condition": "state_change",
      "description": "书房灯状态变化"
    }
  ]
}
```

---

## 4. HAWatcher 守护线程

**位置：** `niu_api/internal/ha_watcher/`

**模式：** 与 scheduler-server 相同 — 写配置文件 → 守护线程读取 → 触发回调 → ChatQueue 推送。

**WebSocket 连接管理：** HAWatcher 维护一个独立的 WebSocket 长连接，与 ha_status 等工具的 REST/WebSocket 调用无关。ha_status 的 WebSocket 调用（device_registry/area_registry）使用短连接：连接 → 认证 → 发送命令 → 接收结果 → 关闭。HAWatcher 的长连接仅用于 subscribe_trigger 事件监听。

**生命周期（由 niu_api 管理，非 MCP Server）：**
1. `niu_api` 启动时检查 `~/.niu/ha-config.json` 是否存在且有效，如有效则自动启动 HAWatcher
2. `ha_setup` 写入配置后调用 `niu_api.internal.ha_watcher.start_watcher()`（MCP Server 不直接启动线程，只写配置 + 调用 niu_api 提供的启动函数）
3. 连接 WebSocket，认证，订阅 triggers
4. 触发时调用 `trigger_callback(description)` → ChatQueue 推送
5. 30 秒心跳（ping/pong）
6. 断线 5 秒自动重连
7. `ha-config.json` 被删除时停止 HAWatcher
8. triggers 列表为空时不建立 WebSocket 连接（仅监控配置文件变化）
9. `niu_api` 关闭时停止 HAWatcher

**重连序列：** 重连时始终先读取最新配置文件，再按最新 triggers 列表订阅，不继承旧连接的订阅状态。

**配置文件监控：**
- ha_subscribe 写入后通过 `threading.Event` 通知 HAWatcher 立即重读（消除轮询延迟）
- 保留 mtime 轮询作为兜底机制（每 5 秒检查），防止 Event 通知丢失
- triggers 增减时动态订阅/取消订阅

**推送格式与 ChatQueue 集成：**
- 触发消息使用 `source="ha-watcher"`, `channel="ha"` 入队（与 scheduler 的 `source="scheduler"`, `channel="scheduler"` 同模式）
- 使用 `enqueue_sync`（fire-and-forget），不等待 Agent 回复
- 推送文本格式：`[智能家居] {description}`
- Agent 系统提示词应包含指引：收到 `[智能家居]` 前缀消息时，主动告知用户并询问是否需要操作

---

## 5. MCP Server 注册

### 5.1 目录结构

```
mcp-servers/ha-server/
├── src/
│   └── niu_ha_server/
│       ├── __init__.py      # TOOL_SCHEMAS + 工具函数
│       └── __main__.py      # 入口点
└── pyproject.toml
```

### 5.2 TOOL_SCHEMAS 规范

每个工具必须定义完整的 `description`（含用途、何时调用、参数说明、使用示例）和 `input_schema`（JSON Schema），遵循现有 MCP Server 模式（参照 scheduler-server / memory-server）。

**工具描述：**

- **ha_status**: `"查询智能家居设备、场景、自动化的当前状态。首次使用或需要了解可用设备时调用。返回按区域分类的设备列表，包含每个设备的可用操作。调用 ha_control 前建议先调用此工具确认设备状态和可用操作。可按 area 或 domain 过滤减少返回量。"`
- **ha_control**: `"控制智能家居设备。需要 entity_id 和 action 参数。entity_id 从 ha_status 获取，action 必须在该设备允许的 actions 列表中。set_brightness 的 value 范围 0-100，set_temperature 的 value 为目标温度。"`
- **ha_subscribe**: `"订阅智能家居设备状态变化通知。支持 state_change（状态变化）、above（超过阈值）、below（低于阈值）三种条件。触发时通过 [智能家居] 前缀消息推送。operation='list' 查看当前订阅，operation='unsubscribe' 取消订阅。"`
- **ha_setup**: `"配置 Home Assistant 连接。首次使用时传入 ha_url 和 ha_token。无参数时返回当前连接状态和订阅列表。ha_token 从 HA Web UI → 用户头像 → Security → Long-Lived Access Tokens 获取。"`
- **ha_integrate**: `"管理 Home Assistant 集成（添加/删除设备品牌集成）。发起配置流时只需 handler 参数，返回表单字段后由 Agent 引导用户填写，再用 flow_id + data 推进。operation='delete' 删除已有集成。"`

**input_schema 示例（ha_subscribe）：**

```python
"input_schema": {
    "type": "object",
    "properties": {
        "entity_id": {"type": "string", "description": "监听的实体 ID，如 sensor.xxx_temperature"},
        "condition": {"type": "string", "enum": ["state_change", "above", "below"], "description": "条件类型"},
        "value": {"type": "number", "description": "above/below 时的阈值"},
        "from_state": {"type": "string", "description": "state_change 起始状态过滤，如 'off'"},
        "to_state": {"type": "string", "description": "state_change 目标状态过滤，如 'on'"},
        "description": {"type": "string", "description": "触发时的描述文本"},
        "trigger_id": {"type": "string", "description": "触发器唯一标识，取消订阅时需要"},
        "operation": {"type": "string", "enum": ["unsubscribe", "list"], "description": "操作类型，不传表示新增"}
    },
    "required": [],
    # 条件必填规则：新增订阅时 entity_id 和 condition 必填；取消订阅时 trigger_id 必填；查询列表时无需其他参数
}
```

**input_schema 示例（ha_integrate）：**

```python
"input_schema": {
    "type": "object",
    "properties": {
        "handler": {"type": "string", "description": "集成域名，如 xiaomi_miot"},
        "flow_id": {"type": "string", "description": "配置流 ID，推进步骤时必填"},
        "data": {"type": "object", "description": "表单数据键值对，如 {\"username\": \"xxx\", \"password\": \"yyy\"}"},
        "operation": {"type": "string", "enum": ["delete"], "description": "操作类型，delete 表示删除集成"},
        "entry_id": {"type": "string", "description": "集成条目 ID，删除时必填"}
    },
    "required": [],
    # 条件必填规则：发起配置流时 handler 必填；推进步骤时 flow_id 和 data 必填；删除时 operation 和 entry_id 必填
}
```

### 5.2 mcp-servers.yaml 配置

```yaml
ha-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_ha_server"
  workdir: mcp-servers/ha-server/src
  preload: true
```

### 5.3 Agent 配置

在 `config/agents/niu.md` 的 `mcpServers` 列表中添加 `ha-server`。

---

## 6. 安装辅助

**不是 ha-server 的功能。** 通过编写手册让 Agent 自己读取，引导用户完成 HA 安装和集成配置。

手册位置：`config/skills/ha-setup-guide.md`

手册内容来源：`~/ha-verify/docs/ha-integration-manual.md`（已完成的验证文档，包含 Docker 安装、HACS 安装、Xiaomi Miot Auto 配置流等完整步骤）。

---

## 7. 审核修复记录

### 第一轮

| 原问题 | 严重度 | 修复方案 |
|--------|--------|----------|
| ha_status 无前置检查 | Critical | 加 connected: false 前置检查，ha-config.json 不存在时返回错误提示 |
| ha_integrate flow_id 非必填导致后续步骤混乱 | High | flow_id 标注为"后续步骤必填"，发起时不传，推进时必传 |
| HAWatcher triggers 为空时仍连接 WebSocket | High | triggers 列表为空时不建立连接，仅监控配置文件变化 |
| 缺少删除集成能力 | High | ha_integrate 增加 action="delete" + entry_id 参数 |
| ha_status 数据量大 | Medium | 接受，Agent 上下文窗口足够；未来需加 area 过滤 |
| brightness 转换硬编码 | Medium | 明确文档说明 value 范围 0-100，内部 ×2.55 转为 HA 的 0-255 |
| 缺少订阅列表查询 | Medium | ha_setup 无参数时返回 triggers 列表 |

### 第二轮

| 原问题 | 严重度 | 修复方案 |
|--------|--------|----------|
| Config Flow REST API body 语法错误 | Critical | 修复 `_options` → `show_options`，加注"实施前需再验证" |
| HAWatcher 读写竞态（无原子写入/文件锁） | Critical | 原子写入（临时文件 + os.rename）+ threading.Event 通知即时重读 |
| 订阅激活延迟（5 秒空窗期） | Critical | threading.Event 替代纯轮询，mtime 轮询仅作兜底 |
| ha_control 无 domain-action 校验 | High | 调用 HA API 前校验 action 是否在 domain 允许列表中 |
| state_change 触发器泛滥（无 from/to 过滤） | High | 增加 from_state/to_state 可选参数，加文档说明无过滤风险 |
| Token 明文存储无安全措施 | High | 文件权限 600，ha_setup 创建时设置 |
| scenes 示例 JSON 语法错误 | High | 修复 `entity.reading_mode` → `"entity_id": "scene.reading_mode"` |
| vacuum 行损坏 | High | 修复 `扫地_on` → `扫地机`，补 `turn_on` |
| Config Flow data_schema 格式未验证 | Medium | 加注 fields 是从 Voluptuous data_schema 转换而来 |
| HAWatcher 重连序列未定义 | Medium | 重连时先读最新配置再订阅，不继承旧订阅 |
| mcp-servers.yaml 格式错误 + workdir 路径不一致 | Medium | 修复 Markdown 格式，workdir 去掉 `../` 前缀 |
| trigger_id 唯一性未保证 | Medium | 改为工具自动生成（`ha_trig_{timestamp}_{random}`），trigger_id 改为可选 |
| ha_status WebSocket 连接管理未指定 | Medium | 明确 ha_status 用短连接，HAWatcher 用独立长连接 |
| ha_control 响应缺 attributes | Medium | 成功响应增加 attributes 字段 |
| ha_setup 无参数行为未定义 | Medium | 明确返回配置状态 + 连接验证 + triggers 列表 |

### 第三轮

| 原问题 | 严重度 | 修复方案 |
|--------|--------|----------|
| 配置文件字段名不一致（value vs threshold） | Critical | 统一配置文件和返回值用 `threshold`，工具参数用 `value`，实现时做映射 |
| action 参数名冲突（ha_subscribe vs ha_control） | Critical | ha_subscribe 和 ha_integrate 的 action 改名为 `operation` |
| HAWatcher → ChatQueue 无 source/channel | Critical | 使用 `source="ha-watcher"`, `channel="ha"`, `enqueue_sync`（fire-and-forget），Agent 提示词加 `[智能家居]` 消息处理指引 |
| HAWatcher 谁启动？（MCP Server vs niu_api） | High | 由 niu_api 管理，ha_setup 调用 `niu_api.internal.ha_watcher.start_watcher()`，MCP Server 只写配置 |
| 缺少 input_schema | High | 添加完整 TOOL_SCHEMAS 规范，含 description + input_schema，参照现有 MCP Server |
| ha_status 无过滤参数（500+ 实体淹没上下文） | High | 增加 `area` 和 `domain` 可选过滤参数 |
| read-modify-write 竞态无锁 | High | 添加模块级 `threading.Lock`，与 memory-server `_memory_file_lock` 同模式 |
| ha_control 响应假设单一实体 | Medium | 实现说明：HA 返回变更实体数组，需查找目标或回退 GET |
| ha_integrate data 参数类型不明确 | Medium | 明确 `data` 为 `type: object`，键值对 |
| 订阅列表隐藏在 ha_setup | Medium | ha_subscribe 增加 `operation="list"` 模式 |
| Token 日志/对话泄露风险 | Medium | ha_token 不得出现在工具返回值或日志中，实现时脱敏 |
| TOOL_SCHEMAS description 缺失 | Medium | 添加 5 个工具的完整描述字符串和 input_schema 示例 |

### 第四轮

| 原问题 | 严重度 | 修复方案 |
|--------|--------|----------|
| ha_subscribe required 字段对 list/unsubscribe 模式不适用 | Critical | required 改为空列表，加条件必填规则注释 |
| unsubscribe 时 trigger_id 实际必填但 schema 未强制 | High | 参数表标注"取消订阅时必填"，input_schema 描述中明确 |
| ha_integrate delete 时 entry_id 未强制 | High | 参数表已有"删除集成时必填"，input_schema 加条件必填规则注释 |
| 配置文件 trigger id 格式不一致（trig_001 vs ha_trig_xxx） | High | 统一为 ha_trig_{timestamp}_{random} 格式 |
| threshold 字段有无未明确 | Medium | 加注"threshold 仅在 above/below 时存在" |
| ha_integrate 缺少 input_schema | Medium | 添加完整 input_schema 示例 |

### 第五轮

| 原问题 | 严重度 | 修复方案 |
|--------|--------|----------|
| Config Flow 推进步骤 body 格式未验证（"action": "account" 可能不正确） | Medium | 移除具体 action 示例，改为"data 参数直接展开"，加注实施时必须用真实 HA 实例验证 body 格式 |
