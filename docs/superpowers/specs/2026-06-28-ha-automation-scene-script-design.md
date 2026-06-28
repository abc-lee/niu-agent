# 智能家居自动化/场景/脚本管理设计

## 目标

在 ha-server MCP 中新增自动化、场景、脚本的 CRUD 能力，让 Agent 用一条自然语言指令就能完成"条件触发"、"多设备切换"、"有序步骤"等智能家居逻辑，无需了解 HA 底层 API 细节。

## 设计原则

**高层封装，不是 HA API 薄包装**。HA 官方 MCP 有 89 个工具，太底层不适合 Agent 使用。我们的工具应该吞掉 entity_id/object_id 映射、API 端点细节、数据格式差异等复杂性，只暴露用户能理解的概念：名字、功能、操作。

但**功能不能减少**——封装是简化 Agent 的理解，不是阉割 HA 的能力。Agent 需要看到完整的 trigger/condition/action schema 才能帮用户编写自动化。

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

- **list**：返回 `[{name, entity_id, state, triggers_summary, actions_summary}, ...]`
- **get/create/update/delete**：参数统一为 `name`（字符串），工具内部通过 `GET /api/states` 过滤 domain 找到 entity_id，再通过 WebSocket `config/entity_registry/list` 找到 `unique_id`（即 config_key），最后调用配置 API

这样 Agent 不需要知道 entity_id 和 object_id 的区别，用户说"看看晚安模式"就够了。但完整配置必须透明可见——Agent 需要看到 triggers/conditions/actions 的完整结构才能帮用户编写和修改自动化。

### 2. list 返回摘要 + 可选完整配置

HA 没有 `/config/automation/config/list` 端点。list 操作用 `GET /api/states` 过滤 domain，返回名称、状态、triggers 摘要（如"湿度>70%, 日落"）、actions 摘要（如"开除湿, 发通知"）。

`detail=true` 参数时，额外通过 WebSocket `automation/config` 逐个获取完整配置（用 `_ws_batch_call` 构建多条命令），让 Agent 一次就能看到所有自动化的完整内容。

### 3. 工具 description 包含完整 schema 参考

磁盘映射的 `long` 字段必须包含 HA 自动化/场景/脚本的完整 schema，让 Agent 不需要额外查询就知道怎么写配置。这是帮用户编写自动化的前提。

数据格式用 HA 2024.8+ 标准：
- 键名用复数：`triggers` / `conditions` / `actions`
- 动作内用 `action` 替代 `service`：`{"action": "light.turn_on", ...}`
- 自动化 config 必须包含 `id` 字段（工具自动生成 UUID hex）

### 4. create 时工具自动生成 object_id 和 id

- 自动化：`object_id` = `uuid4().hex`，config 中的 `id` = 同一个值
- 场景：`object_id` = `uuid4().hex`
- 脚本：`object_id` = 从 alias 生成 slug（小写字母+数字+下划线）

### 5. update 为替换式，非合并式

Agent 先 get 完整配置，修改后传完整 config 给 update。替换式语义清晰——删除某个 trigger 就不在新 config 中包含它。合并式在嵌套结构中语义模糊（数组怎么合并？替换还是追加？），容易出错。

### 6. delete 前返回预览

delete 操作先返回待删除项的名称和摘要信息，Agent 需向用户确认后再传 `confirm=true` 真正执行。防止误删。

## 新增工具

### ha_automation

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete` / `enable` / `disable` / `trigger`

| action | 参数 | 说明 |
|--------|------|------|
| `list` | `detail`(可选) | 列出所有自动化的摘要（名称+状态+触发/动作摘要）；detail=true 返回完整配置 |
| `get` | `name` | 获取完整配置（triggers/conditions/actions/mode） |
| `create` | `name` + `config` | 创建自动化，object_id 自动生成 |
| `update` | `name` + `config` | 替换式更新（传完整 config） |
| `delete` | `name` + `confirm`(可选) | 不带 confirm 返回预览；带 confirm=true 执行删除 |
| `enable` | `name` | 启用自动化 |
| `disable` | `name` | 禁用自动化（不删除） |
| `trigger` | `name` | 手动触发一次 |

`config` JSON 结构（HA 2024.8+ 格式）：
```json
{
  "mode": "single | restart | queued | parallel",
  "triggers": [
    {"platform": "state", "entity_id": "...", "attribute": "...", "from": "...", "to": "..."},
    {"platform": "numeric_state", "entity_id": "...", "above": 28, "below": null},
    {"platform": "time", "at": "08:00:00"},
    {"platform": "time_pattern", "hours": "/1", "minutes": "0", "seconds": "0"},
    {"platform": "sun", "event": "sunset | sunrise", "offset": "-00:30:00"},
    {"platform": "zone", "entity_id": "person.xxx", "zone": "zone.home", "event": "enter | leave"},
    {"platform": "event", "event_type": "...", "event_data": {}},
    {"platform": "template", "value_template": "{{ ... }}"},
    {"platform": "mqtt", "topic": "...", "payload": "..."},
    {"platform": "calendar", "entity_id": "...", "event": "start | end"},
    {"platform": "webhook", "webhook_id": "..."},
    {"platform": "homeassistant", "event": "start | shutdown"},
    {"platform": "geo_location", "source": "...", "zone": "...", "event": "enter | leave"},
    {"platform": "tag", "tag_id": "..."},
    {"platform": "conversation", "command": "..."}
  ],
  "conditions": [
    {"condition": "state", "entity_id": "...", "state": "on"},
    {"condition": "numeric_state", "entity_id": "...", "above": 20, "below": 30},
    {"condition": "time", "after": "09:00", "before": "22:00", "weekday": ["mon","tue","wed"]},
    {"condition": "sun", "after": "sunrise", "before": "sunset"},
    {"condition": "zone", "entity_id": "...", "zone": "zone.home"},
    {"condition": "template", "value_template": "{{ ... }}"},
    {"condition": "trigger", "id": "trigger_id"},
    {"condition": "and", "conditions": [...]},
    {"condition": "or", "conditions": [...]},
    {"condition": "not", "conditions": [...]}
  ],
  "actions": [
    {"action": "light.turn_on", "target": {"entity_id": "..."}, "data": {"brightness_pct": 80}},
    {"action": "climate.set_hvac_mode", "target": {"entity_id": "..."}, "data": {"hvac_mode": "cool"}},
    {"action": "notify.notify", "data": {"message": "..."}},
    {"delay": {"seconds": 30}},
    {"wait_for_trigger": [{"platform": "state", "entity_id": "...", "to": "..."}], "timeout": 60},
    {"wait_template": {"value_template": "{{ ... }}"}, "timeout": 60},
    {"choose": [{"conditions": [...], "sequence": [...]}], "default": [...]},
    {"if": [{"conditions": [...], "sequence": [...]}], "else": [...]},
    {"repeat": {"count": 3, "sequence": [...]}},
    {"parallel": [{"sequence": [...]}, {"sequence": [...]}]},
    {"condition": [...], "sequence": [...]},
    {"variables": {"var1": "value1"}},
    {"event": "custom_event", "event_data": {}},
    {"scene": "scene.xxx"},
    {"stop": "reason"}
  ]
}
```

实现细节：
- `list`：`GET /api/states` 过滤 `automation.*`，返回摘要；`detail=true` 时用 `_ws_batch_call` + 多个 `{"type": "automation/config", "entity_id": "..."}` 获取完整配置
- `get`：name → states 找 entity_id → entity_registry 找 `unique_id`（即 config_key）→ `GET /api/config/automation/config/{config_key}`
- `create`：生成 `config_key = uuid4().hex`，`config.id = config_key`，`POST /api/config/automation/config/{config_key}`
- `update`：`POST /api/config/automation/config/{config_key}`（完整替换）
- `delete`：`confirm=true` 时 `DELETE /api/config/automation/config/{config_key}`
- `enable`/`disable`：调用 `automation.turn_on`/`automation.turn_off` 服务
- `trigger`：调用 `automation.trigger` 服务

### ha_scene

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete` / `activate` / `snapshot`

| action | 参数 | 说明 |
|--------|------|------|
| `list` | `detail`(可选) | 列出所有场景的摘要；detail=true 返回完整配置 |
| `get` | `name` | 获取完整配置（entities 设备状态快照） |
| `create` | `name` + `config` | 创建场景，object_id 自动生成 |
| `update` | `name` + `config` | 替换式更新 |
| `delete` | `name` + `confirm`(可选) | 不带 confirm 返回预览；带 confirm=true 执行删除 |
| `activate` | `name` | 激活场景（将所有设备切到预设状态） |
| `snapshot` | `name` + `entity_ids` | 从当前设备状态创建场景快照并持久化 |

`config` JSON 结构：
```json
{
  "entities": {
    "light.desk": {"state": "on", "brightness": 200, "color_temp_kelvin": 4000},
    "fan.xxx": {"state": "on", "percentage": 30},
    "climate.xxx": {"state": "cool", "temperature": 24},
    "switch.xxx": {"state": "on"},
    "lock.xxx": {"state": "locked"},
    "cover.xxx": {"state": "open", "position": 80}
  }
}
```

实现细节：
- 映射路径同自动化：name → states 找 entity_id → entity_registry 找 `unique_id` → config_key
- `activate`：调用 `scene.turn_on` 服务
- `snapshot`：调用 `scene.create` 服务获取当前状态，再通过 REST API 持久化

### ha_script

操作类型（`action` 参数）：`list` / `get` / `create` / `update` / `delete` / `run`

| action | 参数 | 说明 |
|--------|------|------|
| `list` | `detail`(可选) | 列出所有脚本的摘要；detail=true 返回完整配置 |
| `get` | `name` | 获取完整配置（sequence 步骤序列） |
| `create` | `name` + `config` | 创建脚本，object_id 从 name 生成 slug |
| `update` | `name` + `config` | 替换式更新 |
| `delete` | `name` + `confirm`(可选) | 不带 confirm 返回预览；带 confirm=true 执行删除 |
| `run` | `name` | 执行脚本 |

`config` JSON 结构（HA 2024.8+ 格式，sequence 与自动化 actions 使用相同动作类型）：
```json
{
  "alias": "晚安模式",
  "mode": "single | restart | queued | parallel",
  "sequence": [
    {"action": "light.turn_off", "target": {"entity_id": "all"}},
    {"delay": {"seconds": 5}},
    {"action": "lock.lock", "target": {"entity_id": "lock.door"}},
    {"action": "climate.set_hvac_mode", "target": {"entity_id": "climate.ac"}, "data": {"hvac_mode": "off"}},
    {"choose": [{"conditions": [...], "sequence": [...]}], "default": [...]},
    {"wait_for_trigger": [...], "timeout": 60}
  ]
}
```

实现细节：
- 脚本的 config_key 是 slug 格式（从 name 生成），与 entity_id 直接对应（`script.{slug}`），无需查 entity_registry
- `run`：调用 `script.turn_on` 服务

## 磁盘映射

更新 `config/disk/ha-server.yaml`：

1. `description` 改为包含职责边界的指引
2. 新增 3 个工具条目，每个工具的 `long` 字段包含：
   - 适用场景和与其他工具的区分
   - 完整的 trigger platform 列表及参数
   - 完整的 condition 类型列表
   - 完整的 action 类型列表
   - mode 字段的可选值及含义

目录级 description：
> 智能家居 — 立即执行用 ha_control；定时一次用 scheduler；条件触发持续生效用 ha_automation；多设备瞬间切换用 ha_scene；有序列有延时用 ha_script

## 实现位置

- **代码**：`mcp-servers/ha-server/src/niu_ha_server/__init__.py` — 新增 3 个 TOOL_SCHEMAS + 3 个实现函数，复用现有 `_get_ha_client()` + `_requests` 调用 REST API，`_ws_batch_call()` 调用 WebSocket API
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
- Blueprint 模板机制
- ha_subscribe 增强（组合条件触发、定时触发 — 这些应通过自动化实现）
