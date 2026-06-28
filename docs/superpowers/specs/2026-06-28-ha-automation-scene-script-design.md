# 智能家居自动化/场景/脚本管理设计

## 目标

在 ha-server MCP 中新增自动化、场景、脚本的完整 CRUD 能力，让 Agent 能通过自然语言创建和管理"条件触发"、"多设备切换"、"有序步骤"三类智能家居逻辑。

## 背景

当前 ha-server 只有查询和控制能力（ha_status / ha_control / ha_subscribe / ha_integrate / ha_setup），无法创建、编辑、查看或删除自动化、场景和脚本。用户说"湿度超过 70% 就开除湿"这类需求，Agent 只能用定时任务模拟，无法实现真正的条件触发。

## 工具职责边界

| 用户意图 | 正确工具 | 说明 |
|----------|----------|------|
| 立即执行一次 | `ha_control` | "把客厅灯打开" |
| 定时执行一次 | `scheduler` | "明天 8 点叫我"、"3 小时后关空调" |
| 条件触发、持续生效 | `ha_automation` (新增) | "湿度 > 70% 开除湿"、"日落开灯" |
| 多设备瞬间切换 | `ha_scene` (新增) | "阅读模式"、"晚安模式" |
| 有序列、有延时 | `ha_script` (新增) | "先关灯，等 5 秒，再锁门" |

## 新增工具

### ha_automation

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete`

| action | 必填参数 | 说明 |
|--------|----------|------|
| `list` | 无 | 列出所有自动化的 ID、名称、状态 |
| `get` | `automation_id` | 获取完整配置（trigger/condition/action/mode） |
| `create` | `config` | 创建自动化，config 为 JSON |
| `update` | `automation_id` + `config` | 更新自动化配置（全量替换） |
| `delete` | `automation_id` | 删除自动化 |

`config` JSON 结构：
```json
{
  "alias": "名称",
  "description": "描述（可选）",
  "mode": "single | parallel | queued | restart",
  "trigger": [
    {"platform": "state", "entity_id": "...", "from": "...", "to": "..."},
    {"platform": "numeric_state", "entity_id": "...", "above": 28},
    {"platform": "time", "at": "08:00:00"},
    {"platform": "sun", "event": "sunset", "offset": "-00:30:00"},
    {"platform": "zone", "entity_id": "...", "zone": "zone.home"},
    {"platform": "homeassistant", "event": "start"},
    {"platform": "webhook", "webhook_id": "..."}
  ],
  "condition": [
    {"condition": "state", "entity_id": "...", "state": "on"},
    {"condition": "numeric_state", "entity_id": "...", "below": 30},
    {"condition": "time", "after": "09:00", "before": "22:00"},
    {"condition": "template", "value_template": "{{ ... }}"}
  ],
  "action": [
    {"service": "light.turn_on", "target": {"entity_id": "..."}, "data": {"brightness_pct": 80}},
    {"service": "notify.notify", "data": {"message": "..."}},
    {"delay": {"seconds": 30}},
    {"choose": [{"conditions": [...], "sequence": [...]}], "default": [...]},
    {"repeat": {"count": 3, "sequence": [...]}}
  ]
}
```

HA REST API：
- `GET /api/config/automation/config/list` — 列出所有
- `GET /api/config/automation/config/{object_id}` — 获取单个
- `POST /api/config/automation/config/{object_id}` — 创建/更新
- `DELETE /api/config/automation/config/{object_id}` — 删除

### ha_scene

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete`

| action | 必填参数 | 说明 |
|--------|----------|------|
| `list` | 无 | 列出所有场景的 ID、名称 |
| `get` | `scene_id` | 获取完整配置（entities 设备状态快照） |
| `create` | `config` | 创建场景，config 为 JSON |
| `update` | `scene_id` + `config` | 更新场景配置（全量替换） |
| `delete` | `scene_id` | 删除场景 |

`config` JSON 结构：
```json
{
  "name": "阅读模式",
  "entities": {
    "light.desk": {"state": "on", "brightness": 200, "color_temp_kelvin": 4000},
    "fan.xxx": {"state": "on", "percentage": 30},
    "climate.xxx": {"state": "cool", "temperature": 24}
  }
}
```

HA REST API：
- `GET /api/config/scene/config/list`
- `GET /api/config/scene/config/{object_id}`
- `POST /api/config/scene/config/{object_id}`
- `DELETE /api/config/scene/config/{object_id}`

### ha_script

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete`

| action | 必填参数 | 说明 |
|--------|----------|------|
| `list` | 无 | 列出所有脚本的 ID、名称、状态 |
| `get` | `script_id` | 获取完整配置（sequence 步骤序列） |
| `create` | `config` | 创建脚本，config 为 JSON |
| `update` | `script_id` + `config` | 更新脚本配置（全量替换） |
| `delete` | `script_id` | 删除脚本 |

`config` JSON 结构：
```json
{
  "alias": "晚安模式",
  "mode": "single | parallel | queued | restart",
  "sequence": [
    {"service": "light.turn_off", "target": {"entity_id": "all"}},
    {"delay": {"seconds": 5}},
    {"service": "lock.lock", "target": {"entity_id": "lock.door"}},
    {"service": "climate.set_hvac_mode", "target": {"entity_id": "climate.ac"}, "data": {"hvac_mode": "off"}}
  ]
}
```

HA REST API：
- `GET /api/config/script/config/list`
- `GET /api/config/script/config/{object_id}`
- `POST /api/config/script/config/{object_id}`
- `DELETE /api/config/script/config/{object_id}`

## 磁盘映射

更新 `config/disk/ha-server.yaml`：

1. `description` 改为包含职责边界的指引
2. 新增 `ha_automation`、`ha_scene`、`ha_script` 三个工具条目，每个工具的 `long` 字段写清适用场景和与其他工具的区分

目录级 description：
> 智能家居 — 立即执行用 ha_control；定时一次用 scheduler；条件触发持续生效用 ha_automation；多设备瞬间切换用 ha_scene；有序列有延时用 ha_script

## 实现位置

- **代码**：`mcp-servers/ha-server/src/niu_ha_server/__init__.py` — 新增 3 个 TOOL_SCHEMAS + 3 个实现函数，复用现有 `_ha_request()` 发 REST 请求
- **配置**：`config/disk/ha-server.yaml` — 新增 3 个工具条目 + 更新 description

## 错误处理

- HA 未连接时返回明确错误提示（复用现有 `_ensure_connected()` 检查）
- `config` JSON 格式错误时返回具体字段提示
- HA API 返回非 200 时返回错误码和响应体
- `delete` 操作前确认自动化/场景/脚本存在，不存在返回 404 提示

## 不在范围内

- 历史数据查询（可后续迭代）
- 区域管理 CRUD
- 实体/设备管理
- scene.apply 临时应用（不保存场景的一次性状态切换）
- ha_subscribe 增强（组合条件触发、定时触发 — 这些应通过自动化实现）
