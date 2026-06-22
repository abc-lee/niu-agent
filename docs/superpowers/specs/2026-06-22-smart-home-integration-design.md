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

**无参数。** 调用即返回所有设备、场景、自动化、区域信息。

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
      "entity.reading_mode",
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
| vacuum | 扫地_on, turn_off |
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
{"success": true, "entity_id": "light.xxx", "state": "on"}

// 失败
{"success": false, "error": "实体不存在或服务调用失败: ..."}
```

**实现：** 从 entity_id 提取 domain，映射到 service，调用 `POST /api/services/{domain}/{service}`。set_brightness 额外传 `brightness` 参数（value × 2.55 取整）。set_temperature 额外传 `temperature` 参数。

### 2.3 ha_subscribe — 条件监听与推送

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| entity_id | 是 | 监听的实体 ID |
| condition | 是 | 条件类型：state_change / above / below |
| value | 否 | above/below 时必填，阈值 |
| description | 否 | 触发时的描述文本，默认 "{entity_id} {condition} {value}" |
| trigger_id | 是 | 触发器唯一标识，用于取消订阅 |
| action | 否 | "unsubscribe" 表示取消，不传表示新增 |

**新增订阅流程：**
1. 写入 `~/.niu/ha-config.json` 的 triggers 列表
2. HAWatcher 检测到配置变更，创建 subscribe_trigger 连接
3. 触发时通过 `trigger_callback` → ChatQueue 推送：`[智能家居] {description}`

**取消订阅流程：**
1. 从 `~/.niu/ha-config.json` 的 triggers 列表移除
2. HAWatcher 检测到配置变更，取消对应订阅

**condition → trigger 映射：**

| condition | trigger 配置 |
|-----------|-------------|
| state_change | `{"platform": "state", "entity_id": "..."}` |
| above | `{"platform": "numeric_state", "entity_id": "...", "above": value}` |
| below | `{"platform": "numeric_state", "entity_id": "...", "below": value}` |

**返回：**

```json
// 新增成功
{"success": true, "trigger_id": "trig_001", "message": "已订阅: 温度超过25度"}

// 取消成功
{"success": true, "trigger_id": "trig_001", "message": "已取消订阅"}

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
- **有参数时：** 验证连接（`GET /api/`），成功后写入 `~/.niu/ha-config.json`，启动/重启 HAWatcher
- **无参数时：** 返回当前配置状态

**返回：**

```json
// 配置成功
{"connected": true, "ha_url": "http://localhost:8123", "version": "2026.6.0"}

// 未配置
{"connected": false, "error": "未配置 Home Assistant"}

// 连接失败
{"connected": false, "error": "无法连接到 Home Assistant: ..."}
```

### 2.5 ha_integrate — 集成配置流

**参数：**

| 参数 | 必填 | 说明 |
|------|------|------|
| handler | 是 | 集成域名，如 `xiaomi_miot` |
| flow_id | 否 | 配置流 ID（后续步骤必填） |
| data | 否 | 表单数据后续步骤必填） |
| action | 否 | "delete" 表示删除集成 |
| entry_id | 否 | 删除集成时必填 |

**多步配置流流程：**

1. **发起：** `ha_integrate(handler="xiaomi_miot")` → 调用 `POST /api/config/config_entries/flow` → 返回第一步表单字段
2. **推进：** `ha_integrate(handler="xiaomi_miot", flow_id="xxx", data={"username": "...", "password": "..."})` → 调用 `POST /api/config/config_entries/flow/{flow_id}` → 返回下一步或 create_entry
3. **重复步骤 2** 直到返回 `create_entry`
4. **删除：** `ha_integrate(action="delete", entry_id="xxx")` → 调用 `DELETE /api/config/config_entries/entry/{entry_id}`

**返回格式：**

```json
// 表单步骤
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

**Config Flow REST API（HA 2026.6 验证通过）：**
- 发起：`POST /api/config/config_entries/flow`，body `{"handler": "xiaomi_miot",_options": false}`
- 推进：`POST /api/config/config_entries/flow/{flow_id}`，body `{"action": "account", ...data}`
- 删除：`DELETE /api/config/config_entries/entry/{entry_id}`

**注意：** HA 2026.6 中 Config Flow 的 WebSocket API 已废弃，必须使用 REST API。

---

## 3. 配置文件

**路径：** `~/.niu/ha-config.json`

```json
{
  "ha_url": "http://localhost:8123",
  "ha_token": "eyJhbGciOi...",
  "triggers": [
    {
      "id": "trig_001",
      "entity_id": "sensor.miaomiaoce_t1_4d15_temperature",
      "condition": "above",
      "threshold": 25,
      "description": "温度超过25度"
    },
    {
      "id": "trig_002",
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

**生命周期：**
1. `ha_setup` 成功后启动 HAWatcher
2. 连接 WebSocket，认证，订阅 triggers
3. 触发时调用 `trigger_callback(description)` → ChatQueue 推送
4. 30 秒心跳（ping/pong）
5. 断线 5 秒自动重连
6. `ha-config.json` 被删除时停止 HAWatcher
7. triggers 列表为空时不建立 WebSocket 连接（仅监控配置文件变化）

**配置文件监控：**
- 定期（每 5 秒）检查 `~/.niu/ha-config.json` 的 mtime
- mtime 变化时重新加载 triggers 列表
- triggers 增减时动态订阅/取消订阅

**推送格式：**
```
[智能家居] 温度超过25度
[智能家居] 书房灯状态变化
```

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

### 5.2 mcp-servers.yaml 配置

```yaml-server:
  command: ${PYTHON_PATH}
  args:
    - "-m"
    - "niu_ha_server"
  workdir: ../mcp-servers/ha-server/src
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

| 原问题 | 严重度 | 修复方案 |
|--------|--------|----------|
| ha_status 无前置检查 | Critical | 加 connected: false 前置检查，ha-config.json 不存在时返回错误提示 |
| ha_integrate flow_id 非必填导致后续步骤混乱 | High | flow_id 标注为"后续步骤必填"，发起时不传，推进时必传 |
| HAWatcher triggers 为空时仍连接 WebSocket | High | triggers 列表为空时不建立连接，仅监控配置文件变化 |
| 缺少删除集成能力 | High | ha_integrate 增加 action="delete" + entry_id 参数 |
| ha_status 数据量大 | Medium | 接受，Agent 上下文窗口足够；未来需加 area 过滤 |
| brightness 转换硬编码 | Medium | 明确文档说明 value 范围 0-100，内部 ×2.55 转为 HA 的 0-255 |
| 缺少订阅列表查询 | Medium | ha_status 返回中包含当前 triggers 列表（从 ha-config.json 读取） |
