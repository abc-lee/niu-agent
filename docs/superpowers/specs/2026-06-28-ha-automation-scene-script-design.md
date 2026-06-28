# 智能家居自动化/场景/脚本管理设计

## 目标

在 ha-server MCP 中新增自动化、场景、脚本的 CRUD 能力，让 Agent 用一条自然语言指令就能完成"条件触发"、"多设备切换"、"有序步骤"等智能家居逻辑，无需了解 HA 底层 API 细节。

## 设计原则

**高层封装，不是 HA API 薄包装**。HA 官方 MCP 有 89 个工具，太底层不适合 Agent 使用。我们的工具应该吞掉 entity_id/object_id 映射、API 端点细节、数据格式差异等复杂性，只暴露用户能理解的概念：名字、功能、操作。

## 工具职责边界

| 用户意图 | 正确工具 | 说明 |
|----------|----------|------|
| 立即执行一次 | `ha_control` | "把客厅灯打开" |
| 定时执行一次 | `scheduler` | "明天 8 点叫我"、"3 小时后关空调" |
| 条件触发、持续生效 | `ha_automation` (新增) | "湿度 > 70% 开除湿"、"日落开灯" |
| 多设备瞬间切换 | `ha_scene` (新增) | "阅读模式"、"晚安模式" |
| 有序列、有延时 | `ha_script` (新增) | "先关灯，等 5 秒，再锁门" |

## 核心设计决策

### 1. 统一用名称（alias/name）作为标识

Agent 和用户都用名称引用自动化/场景/脚本，不用 entity_id 或 object_id。工具内部处理映射：

- **list**：返回 `[{name, entity_id, state}, ...]`
- **get/create/update/delete**：参数统一为 `name`（字符串），工具内部通过 `GET /api/states` 过滤 domain 找到 entity_id，再通过 WebSocket `config/entity_registry/list` 找到 config_object_id，最后调用配置 API

这样 Agent 不需要知道 entity_id 和 object_id 的区别，用户说"看看晚安模式"就够了。

### 2. list 用 `/api/states` 过滤 domain

HA 没有 `/config/automation/config/list` 端点。list 操作直接用 `GET /api/states` 过滤 `automation.*` / `scene.*` / `script.*` 实体，返回名称和状态。用户想看完整配置时再用 get 拉取。

### 3. 数据格式用 HA 最新标准

- 键名用复数：`triggers` / `conditions` / `actions`（非弃用的单数形式）
- 动作内用 `action` 替代 `service`：`{"action": "light.turn_on", ...}`
- 自动化 config 必须包含 `id` 字段（工具自动生成 UUID hex）

### 4. create 时工具自动生成 object_id 和 id

- 自动化：`object_id` = `uuid4().hex`，config 中的 `id` = 同一个值
- 场景：`object_id` = `uuid4().hex`
- 脚本：`object_id` = 从 alias 生成 slug（小写字母+数字+下划线）

### 5. delete 前返回预览

delete 操作先返回待删除项的名称和摘要信息，Agent 需向用户确认后再传 `confirm=true` 真正执行。防止误删。

## 新增工具

### ha_automation

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete`

| action | 参数 | 说明 |
|--------|------|------|
| `list` | 无 | 列出所有自动化的名称、entity_id、状态、last_triggered |
| `get` | `name` | 获取完整配置（triggers/conditions/actions/mode） |
| `create` | `name` + `config` | 创建自动化，object_id 自动生成 |
| `update` | `name` + `config` | 更新自动化（先获取当前配置合并后再提交，防止丢失字段） |
| `delete` | `name` + `confirm`(可选) | 不带 confirm 返回预览；带 confirm=true 执行删除 |

`config` JSON 结构（HA 2024.8+ 格式）：
```json
{
  "mode": "single",
  "triggers": [
    {"platform": "state", "entity_id": "sensor.humidity", "above": 70},
    {"platform": "time", "at": "08:00:00"},
    {"platform": "sun", "event": "sunset"},
    {"platform": "numeric_state", "entity_id": "sensor.temp", "above": 28},
    {"platform": "zone", "entity_id": "person.xxx", "zone": "zone.home"}
  ],
  "conditions": [
    {"condition": "state", "entity_id": "input_boolean.home", "state": "on"},
    {"condition": "time", "after": "09:00", "before": "22:00"}
  ],
  "actions": [
    {"action": "climate.set_hvac_mode", "target": {"entity_id": "climate.ac"}, "data": {"hvac_mode": "dry"}},
    {"action": "notify.notify", "data": {"message": "湿度已超过70%，已开除湿"}},
    {"delay": {"seconds": 30}},
    {"choose": [{"conditions": [...], "sequence": [...]}], "default": [...]}
  ]
}
```

实现细节：
- `list`：`GET /api/states` 过滤 `automation.*`，返回 `[{name, entity_id, state, last_triggered}]`
- `get`：name → 通过 states 找 entity_id → entity_registry 找 config_object_id → `GET /api/config/automation/config/{object_id}`
- `create`：生成 `object_id = uuid4().hex`，`config.id = object_id`，`POST /api/config/automation/config/{object_id}`
- `update`：先 get 当前配置，深度合并用户提供的 config，再 POST
- `delete`：先 get 返回预览，`confirm=true` 时 `DELETE /api/config/automation/config/{object_id}`

### ha_scene

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete`

| action | 参数 | 说明 |
|--------|------|------|
| `list` | 无 | 列出所有场景的名称、entity_id |
| `get` | `name` | 获取完整配置（entities 设备状态快照） |
| `create` | `name` + `config` | 创建场景，object_id 自动生成 |
| `update` | `name` + `config` | 更新场景 |
| `delete` | `name` + `confirm`(可选) | 不带 confirm 返回预览；带 confirm=true 执行删除 |

`config` JSON 结构：
```json
{
  "entities": {
    "light.desk": {"state": "on", "brightness": 200, "color_temp_kelvin": 4000},
    "fan.xxx": {"state": "on", "percentage": 30},
    "climate.xxx": {"state": "cool", "temperature": 24}
  }
}
```

实现细节同自动化，API 路径中 `automation` 换成 `scene`。

### ha_script

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete`

| action | 参数 | 说明 |
|--------|------|------|
| `list` | 无 | 列出所有脚本的名称、entity_id、状态 |
| `get` | `name` | 获取完整配置（sequence 步骤序列） |
| `create` | `name` + `config` | 创建脚本，object_id 从 name 生成 slug |
| `update` | `name` + `config` | 更新脚本 |
| `delete` | `name` + `confirm`(可选) | 不带 confirm 返回预览；带 confirm=true 执行删除 |

`config` JSON 结构（HA 2024.8+ 格式）：
```json
{
  "alias": "晚安模式",
  "mode": "single",
  "sequence": [
    {"action": "light.turn_off", "target": {"entity_id": "all"}},
    {"delay": {"seconds": 5}},
    {"action": "lock.lock", "target": {"entity_id": "lock.door"}},
    {"action": "climate.set_hvac_mode", "target": {"entity_id": "climate.ac"}, "data": {"hvac_mode": "off"}}
  ]
}
```

实现细节：
- 脚本的 object_id 是 slug 格式（从 name 生成），与 entity_id 直接对应（`script.{slug}`），无需额外映射
- API 路径中 `automation` 换成 `script`
- 脚本不需要 `id` 字段

## 磁盘映射

更新 `config/disk/ha-server.yaml`：

1. `description` 改为包含职责边界的指引
2. 新增 3 个工具条目，每个工具的 `long` 字段写清适用场景和与其他工具的区分

目录级 description：
> 智能家居 — 立即执行用 ha_control；定时一次用 scheduler；条件触发持续生效用 ha_automation；多设备瞬间切换用 ha_scene；有序列有延时用 ha_script

## 实现位置

- **代码**：`mcp-servers/ha-server/src/niu_ha_server/__init__.py` — 新增 3 个 TOOL_SCHEMAS + 3 个实现函数，复用现有 `_get_ha_client()` + `_requests` 调用 HA API
- **配置**：`config/disk/ha-server.yaml` — 新增 3 个工具条目 + 更新 description

## 错误处理

- HA 未连接时返回明确错误提示（复用现有 `_get_ha_client()` 检查模式）
- 名称找不到时返回"未找到名为 xxx 的自动化/场景/脚本，请先 list 查看可用列表"
- `config` JSON 格式错误时返回具体字段提示
- HA API 返回非 200 时返回错误码和响应体
- HA token 非管理员时提示"此操作需要管理员权限的 HA token"

## 不在范围内

- 历史数据查询（可后续迭代）
- 区域管理 CRUD
- 实体/设备管理
- scene.apply 临时应用
- ha_subscribe 增强（组合条件触发、定时触发 — 这些应通过自动化实现）
