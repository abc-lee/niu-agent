# Home Assistant 智能家居集成 — 验证手册

> 本文档是主 Agent 协助用户安装、配置、使用 HA 的完整操作手册。
> 包含所有已验证的 API 行为、配置方法、接入模式、自动化/场景/脚本管理、踩坑记录。

## 1. Docker 安装与配置

### 1.1 前置条件

- Docker Desktop 已安装并运行（macOS: `brew install --cask docker`，或从 docker.com 下载）
- VPN 已开启（拉取 `ghcr.io` 镜像需要）
- 端口 8123 未被占用

### 1.2 创建 HA 容器

**当前配置（桥接网络 — 无法自动发现局域网设备）：**

```bash
mkdir -p ~/ha-config
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  -e TZ=Asia/Shanghai \
  -v ~/ha-config:/config \
  -p 8123:8123 \
  ghcr.io/home-assistant/home-assistant:stable
```

**推荐配置（host 网络 — 支持自动发现局域网设备）：**

```bash
mkdir -p ~/ha-config
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  --network=host \
  -e TZ=Asia/Shanghai \
  -v ~/ha-config:/config \
  ghcr.io/home-assistant/home-assistant:stable
```

> **重要**：macOS 上 Docker 的 `--network=host` 不生效（Docker Desktop 限制），只有 Linux 上才能用 host 网络。
> macOS 上的替代方案：在 HA 配置中手动指定 `discovery:` 和 `zeroconf:` 开启，但发现能力受限。
> 生产环境建议在 Linux 服务器上运行 HA。

### 1.3 等待 HA 启动

```bash
# 等待 API 可用（时长视你的机器与网络环境而定）
until curl -s http://localhost:8123/api/ > /dev/null 2>&1; do
  sleep 10
done
```

### 1.4 首次设置（Onboarding）

浏览器打开 `http://localhost:8123`，完成：
1. 创建管理员用户名和密码
2. 设置家庭位置（可跳过）
3. 分享匿名统计（可跳过）

### 1.5 创建 Long-Lived Access Token

1. 左下角用户头像 → Security
2. Long-Lived Access Tokens → Create Token
3. 名称填 `agent-test`（或其他标识），复制 Token
4. **Token 只显示一次，必须立即保存**

调用 `ha_setup` 工具传入 `ha_url` 和 `ha_token` 参数，系统会持久化到 `~/.niu/ha-config.json`：

```
/ha/ha_setup --ha-url http://localhost:8123 --ha-token <复制的Token>
```

> **注意**：`ha_setup` 不读取 `os.environ`/`HA_URL`/`HA_TOKEN` 环境变量。配置仅通过工具参数写入 `~/.niu/ha-config.json`。后续无参数调用 `ha_setup` 可查询当前连接状态。

## 2. 模拟实体配置

HA 的 `configuration.yaml` 可以定义模拟实体用于测试，无需真实硬件：

> **注意**：`input_boolean`/`input_number`/`input_select`/`input_button` 等模拟实体仅供 REST API 测试（`/api/states`），`ha_status` 工具会通过 `EXCLUDED_DOMAINS` 过滤掉这些 domain，永远不返回。真实设备接入后请用真实 domain（`light`/`climate`/`sensor` 等）。

```yaml
# ~/ha-config/configuration.yaml
default_config:

# 模拟开关
input_boolean:
  virtual_light:
    name: "虚拟灯"
    icon: mdi:lightbulb
  virtual_fan:
    name: "虚拟风扇"
    icon: mdi:fan
  virtual_door_lock:
    name: "虚拟门锁"
    icon: mdi:door-closed-lock

# 模拟数值
input_number:
  virtual_brightness:
    name: "虚拟亮度"
    min: 0
    max: 100
    step: 1
    icon: mdi:brightness-6
  virtual_temperature:
    name: "虚拟温度"
    min: 16
    max: 30
    step: 0.5
    unit_of_measurement: "°C"
    icon: mdi:thermometer

# 模拟选择
input_select:
  virtual_mode:
    name: "虚拟模式"
    options: ["舒适", "节能", "睡眠"]
    icon: mdi:format-list-bulleted

# 模拟按钮
input_button:
  virtual_reset:
    name: "虚拟重置"
    icon: mdi:refresh
```

修改后需要重启 HA：`docker restart homeassistant`

## 3. API 行为记录

### 3.1 REST API

**基础端点：**

| 端点 | 方法 | 说明 | 状态码 |
|------|------|------|--------|
| `/api/` | GET | 健康检查 | 200 |
| `/api/config` | GET | HA 配置信息 | 200 |
| `/api/states` | GET | 所有实体状态 | 200 |
| `/api/states/{entity_id}` | GET | 单个实体 | 200/404 |
| `/api/events` | GET | 所有事件类型 | 200 |
| `/api/services` | GET | 所有服务 | 200 |
| `/api/history/period/{timestamp}` | GET | 实体历史 | 200 |
| `/api/logbook` | GET | 事件日志 | 200 |

**设备控制：**

| 操作 | 端点 | 请求体 |
|------|------|--------|
| 开灯 | `POST /api/services/input_boolean/turn_on` | `{"entity_id": "input_boolean.virtual_light"}` |
| 关灯 | `POST /api/services/input_boolean/turn_off` | `{"entity_id": "..."}` |
| 切换 | `POST /api/services/input_boolean/toggle` | `{"entity_id": "..."}` |
| 设数值 | `POST /api/services/input_number/set_value` | `{"entity_id": "...", "value": 75}` |
| 选模式 | `POST /api/services/input_select/select_option` | `{"entity_id": "...", "option": "节能"}` |

**行为特征（踩坑记录）：**

1. **无效服务返回 400 而非 404** — `POST /api/services/nonexistent/xxx` 返回 `400 Bad Request`，不是 404
2. **无效 Token 返回 401** — 正确
3. **不存在的实体返回 404** — 正确
4. **input_number 状态是浮点数字符串** — 设 `value: 42`，状态返回 `"42.0"` 而非 `"42"`
5. **认证头格式** — `Authorization: Bearer {TOKEN}`

### 3.2 WebSocket API

**连接地址：** `ws://localhost:8123/api/websocket`（HTTP）或 `wss://...`（HTTPS）

**认证流程：**
1. 连接后收到 `{"type": "auth_required", "ha_version": "2026.6.4"}`
2. 发送 `{"type": "auth", "access_token": TOKEN}`
3. 收到 `{"type": "auth_ok"}` 或 `{"type": "auth_invalid"}`

**基础命令：**

| 命令 | 请求 | 响应 |
|------|------|------|
| get_states | `{"id":1, "type":"get_states"}` | `{"id":1, "type":"result", "success":true, "result":[...]}` |
| get_services | `{"id":2, "type":"get_services"}` | `{"success":true, "result":{"domain":{...}}}` |
| get_config | `{"id":3, "type":"get_config"}` | `{"success":true, "result":{...}}` |
| call_service | `{"id":4, "type":"call_service", "domain":"...", "service":"...", "service_data":{...}}` | `{"success":true}` |
| ping | `{"id":5, "type":"ping"}` | `{"id":5, "type":"pong"}` |
| subscribe_events | `{"id":6, "type":"subscribe_events", "event_type":"state_changed"}` | 持续推送 event |
| unsubscribe_events | `{"id":7, "type":"unsubscribe_events", "subscription":6}` | `{"success":true}` |

**subscribe_trigger（条件推送 — 核心能力）：**

```json
// 订阅状态触发器
{"id": 8, "type": "subscribe_trigger",
 "trigger": {"platform": "state", "entity_id": "input_boolean.virtual_light", "from": "off", "to": "on"}}

// 订阅数值触发器
{"id": 9, "type": "subscribe_trigger",
 "trigger": {"platform": "numeric_state", "entity_id": "input_number.virtual_brightness", "above": 80}}

// 订阅组合条件（template trigger）
{"id": 10, "type": "subscribe_trigger",
 "trigger": {"platform": "template",
  "value_template": "{{ is_state('input_boolean.virtual_door_lock', 'on') and states('input_number.virtual_temperature') | float > 25 }}"}}
```

**⚠️ Trigger 事件格式（踩坑）：**

Trigger 事件**不是** `event_type: "trigger"`，而是通过 `event.variables.trigger` 识别：

```json
{
    "id": 8,
    "type": "event",
    "event": {
        "variables": {
            "trigger": {
                "platform": "state",
                "entity_id": "input_boolean.virtual_light",
                "from_state": {"state": "off"},
                "to_state": {"state": "on"}
            }
        },
        "context": {...}
    }
}
```

**判断逻辑**：`"trigger" in msg.get("event", {}).get("variables", {})`

**Trigger 事件里的通用字段（`event.variables.trigger`）：**

| 字段 | 含义 | 备注 |
|------|------|------|
| `platform` | 触发器类型（`state` / `numeric_state` / `template` …） | 与订阅时写的 `platform` 一致 |
| `entity_id` | 触发的实体 | `template` 触发器可能没有 |
| `from_state` / `to_state` | 变化前后的 State 对象 | `state` / `numeric_state` 通常携带，`template` 可能没有；程序只取 `.state`，不转发 `attributes` |
| 自定义键 | 写在 `trigger.variables` 里的字段 | HA 渲染后合并到 `event.variables` 顶层，与 `trigger` 平级 |

**⚠️ `variables` 必须写在 `trigger` 体内：**

```json
{"id": 11, "type": "subscribe_trigger",
 "trigger": {"platform": "state", "entity_id": "sensor.lock_action", "to": "outside_unlock",
             "variables": {"lock_method": "{{ states('sensor.lock_method') }}"}}}
```

HA 先渲染 `trigger` 体内 `variables` 的 `{{ ... }}`，再把渲染结果合并进事件：

```json
{"id": 11, "type": "event", "event": {"variables": {
    "trigger": {"platform": "state", "entity_id": "sensor.lock_action", "to_state": {"state": "outside_unlock"}},
    "lock_method": "指纹"
}}}
```

**`subscribe_trigger` 顶层的 `variables` 不会回到事件**（HA 只把它放进 `TriggerInfo`，用于 `enabled` 等元信息），因此不是可用参数。

**call_service 与 trigger 事件交错问题：**

调用 `call_service` 时，WebSocket 会先返回 `result` 消息，再返回 `trigger` event 消息（如果触发了条件）。必须用基于 msg ID 或消息类型的匹配逻辑来正确接收两者，不能假设单一 send→recv 映射。

**心跳保活：**

30秒无消息时发送 ping，HA 返回 pong。超过约2分钟无活动连接可能被断开。

### 3.3 Config Flow REST API（HA 2026.6 验证通过）

**⚠️ WebSocket API 的 Config Flow 命令（`config/integration/initialize` 等）在 HA 2026.6 中返回 `unknown_command`，已废弃或移除。**

**正确方式 — REST API：**

| 操作 | 方法 | 端点 | 请求体 |
|------|------|------|--------|
| 获取已有集成 | GET | `/api/config/config_entries/entry` | - |
| 获取可用集成列表 | GET | `/api/config/integration/list` | - |
| 发起配置流 | POST | `/api/config/config_entries/flow` | 见下文 |
| 获取集成清单 | GET | `/api/config/integration/list` | - |

**发起配置流：**

```bash
POST /api/config/config_entries/flow
Content-Type: application/json

{
    "handler": "xiaomi_miot",       # 集成域名
    "show_advanced_options": false,
    "context": {"source": "user"}   # 来源：user=手动, zeroconf=自动发现, dhcp=DHCP发现
}
```

**响应格式（第一步 — 要求输入）：**

```json
{
    "type": "form",
    "flow_id": "abc123",
    "handler": "xiaomi_miot",
    "step_id": "user",
    "data_schema": [
        {"name": "username", "required": true, "type": "string"},
        {"name": "password", "required": true, "type": "string"}
    ],
    "description_placeholders": {}
}
```

**推进配置流（提交表单）：**

```bash
POST /api/config/config_entries/flow/{flow_id}
Content-Type: application/json

{
    "username": "user@example.com",
    "password": "xxx"
}
```

**响应格式（成功）：**

```json
{
    "type": "create_entry",
    "entry_id": "xyz789",
    "title": "米家",
    "data": {...}
}
```

**响应格式（需要更多步骤）：**

```json
{
    "type": "form",
    "step_id": "select_device",
    "data_schema": [...]
}
```

**响应格式（需要外部操作）：**

```json
{
    "type": "external",
    "url": "https://oauth.example.com/..."
}
```

### 3.4 自动发现机制

HA 内置4种发现机制，当前测试环境已加载：

| 机制 | 说明 | 发现对象 |
|------|------|----------|
| zeroconf | mDNS/DNS-SD | 局域网设备（打印机、Chromecast、ESPHome 等） |
| dhcp | DHCP 请求监听 | 新接入网络的设备 |
| ssdp | UPnP/SSDP | 网络设备（路由器、媒体服务器等） |
| bluetooth | BLE 扫描 | 蓝牙设备（传感器、灯泡等） |

**发现事件格式：**

当 HA 发现新设备时，推送 `homeassistant_discovery` 事件，包含设备信息和可用的集成域名。

**⚠️ Docker 网络限制：**
- 桥接网络模式（默认）无法监听局域网流量，自动发现不工作
- host 网络模式可正常发现，但 macOS Docker Desktop 不支持 `--network=host`
- Linux 生产环境建议使用 host 网络

### 3.5 注册表查询（WebSocket）

| 命令 | 说明 | 返回 |
|------|------|------|
| `config/device_registry/list` | 所有设备 | 设备列表（name, model, manufacturer, via_device_id） |
| `config/area_registry/list` | 所有区域 | 区域列表 |
| `config/entity_registry/list` | 所有实体注册信息 | 实体列表（platform, device_id, area_id） |

## 4. 各品牌接入模式

### 4.1 已验证的接入模式

通过 Config Flow REST API 逐个测试，各品牌的接入模式如下：

| 品牌/集成 | 类型 | 第一步字段 | Agent 自动化程度 | 需要用户配合 |
|-----------|------|-----------|-----------------|-------------|
| **mqtt** | 账号密码 | host, port, username, password | 90% | 提供 broker 地址和账密 |
| **yeelight** | 局域网直连 | host | 95% | 确认设备 IP（自动发现可覆盖） |
| **esphome** | 局域网直连 | host, port | 95% | 确认设备地址 |
| **deconz** | 局域网直连 | host, port | 90% | 提供网关地址 |
| **xiaomi_miot** | 账号密码 + 局域网直连 | account: username, password, server_country, conn_mode; token: host, token | 90% | 提供米家账密或设备IP/Token |
| **philips_hue** | 局域网直连 | host | 85% | 按桥接器按钮配对 |
| **tuya** | OAuth | user_code | 30% | 浏览器完成 OAuth |
| **sonoff** | 账号密码 | username, password | 90% | 提供 Sonoff 账密 |
| **zha** | 物理配对 | serial_port, radio_type | 20% | 插入 Zigbee dongle + 配对 |
| **homekit** | 物理配对 | pin | 40% | 扫描配对码 |
| **matter** | 新标准 | url | 50% | 设备配对 |

### 4.2 接入模式分类

**模式 1: 账号密码型（Agent 可全自动）**
- 代表: xiaomi_miot, mqtt, sonoff
- 流程: initialize → 用户口述账密 → Agent 填入 → create_entry
- 自动化程度: 90%
- Agent 只需问用户要账密

**模式 2: 局域网直连型（自动发现 + 确认）**
- 代表: yeelight, esphome, deconz, philips_hue
- 流程: zeroconf 自动发现 → Agent 展示发现结果 → 用户确认 → create_entry
- 自动化程度: 85-95%
- 用户只需说"对，就是这个设备"

**模式 3: OAuth 认证型（需浏览器自动化）**
- 代表: tuya, google, ecovacs
- 流程: initialize → 返回 external URL → browser-server 自动完成 OAuth
- 自动化程度: 30-60%
- 需要 browser-server 协助，用户可能需要扫码或确认

**模式 4: 物理配对型（需用户操作硬件）**
- 代表: zha, homekit, bluetooth
- 流程: initialize → 提示用户按按钮/扫码 → Agent 检测到配对 → create_entry
- 自动化程度: 10-40%
- 用户必须物理操作设备

### 4.3 如何查询某个集成的接入模式

```bash
# 发起该集成的 Config Flow，查看 data_schema 中的字段
curl -X POST http://localhost:8123/api/config/config_entries/flow \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"handler": "目标集成域名", "show_advanced_options": false, "context": {"source": "user"}}'
```

返回的 `data_schema` 数组就是需要填写的字段列表，Agent 据此决定：
- 只有 host/port → 局域网直连型
- 有 username/password → 账号密码型
- type 为 "external" → OAuth 型
- 需要串口/pin → 物理配对型

## 5. 条件推送机制（核心能力）

### 5.1 工作原理

1. Agent 通过 `subscribe_trigger` 向 HA 注册条件（如"湿度>60%"）
2. HA 仅在条件满足时推送 trigger 事件
3. Agent 收到事件后执行后续动作

**推送通道（与定时任务一致）**：
- 订阅触发事件（程序消息）写入 Message.DB 唤醒主 Agent——**不直接推 IM**；Chat 页面由 DB 变更 SSE 刷新显示（前端逻辑，同定时提醒）
- ha-watcher 与定时任务同走 chat_queue 后台触发分支（`else` 分支置 IM 标志为真——`set_im_force(True)`）
- 主 Agent 收到事件后的话（如"湿度已超过 60%了"）经 `should_push_im` 闸门投递 IM（chat_queue 特判 `send_sync` + 流式卡片）

### 5.2 三种触发器类型

**状态触发器（精确 from→to 过滤）：**
```json
{"platform": "state", "entity_id": "input_boolean.virtual_light", "from": "off", "to": "on"}
```
- `from`/`to` 可省略（任意变化都触发）
- 精确匹配：on→on 不会触发 off→on 的触发器

**数值触发器（阈值跨越）：**
```json
{"platform": "numeric_state", "entity_id": "input_number.virtual_temperature", "above": 25}
```
- 只在**跨越**阈值时触发（从24升到26触发，从26升到28不触发）
- 支持 `above` 和 `below`
- 状态值必须是数字

**模板触发器（组合条件）：**
```json
{"platform": "template", "value_template": "{{ is_state('input_boolean.virtual_door_lock', 'on') and states('input_number.virtual_temperature') | float > 25 }}"}
```
- 支持任意 Jinja2 表达式
- 可组合多个条件
- ⚠️ 响应延迟较大（视你的环境而定），测试时 timeout 需放宽设置

### 5.3 已验证场景

| 场景 | 测试项 | 结果 |
|------|--------|------|
| 湿度超过阈值通知 | numeric_state above | PASS |
| 低于阈值不通知 | numeric_state 不跨越 | PASS |
| 条件满足后执行动作 | trigger → call_service | PASS |
| 门锁+温度组合条件 | template trigger | PASS |
| 条件不完全满足不通知 | template 部分满足 | PASS |

### 5.4 断线重连

- WebSocket 断开后自动重连：已验证通过
- 空闲保活（ping/pong）：客户端主动保活，防 HA 因闲置断开长连接；长连接能否稳定维持以你环境为准
- Docker restart 后自动恢复：已验证通过

### 5.5 推送内容与 variables（0.5.5 起）

**程序推送格式**：ha_watcher 收到 trigger 事件后，把订阅的 `description` 与事件里的通用字段拼成一条消息，以 `[智能家居]` 前缀写入 Message.DB 唤醒主 Agent：

```text
[智能家居] 门锁：有人从门外开锁
实体: sensor.lock_action
触发类型: state
状态变化: inside_unlock -> outside_unlock
附加: lock_method=指纹, lock_key_id=2147549186
时间: 2026-10-20 14:32:07
```

- `实体 / 触发类型 / 状态变化 / 附加` 都是按需出现：事件里缺哪个字段就省略哪一行，不抛异常；`template` 触发器通常只有 `触发类型` 与 `时间`
- `状态变化` 只取 `from_state.state` / `to_state.state`，`attributes` 不进入推送
- `附加` 最多 10 项；实体名与每个值先折叠换行再截断到 200 字符
- `时间` 由程序本地生成（HA 的 trigger 事件不带 `time_fired`）

**variables 用法**：`ha_subscribe` 的 `variables`（JSON 对象）被写进 `trigger` 体内，由 HA 渲染 `{{ ... }}` 后随事件带回，程序不自行渲染模板；值可以是字面量，也可以是 HA 模板。

**重启/恢复过滤只对 state trigger 生效**：启动回填只按 `platform == "state"` 判定并丢弃（如 `unknown -> unknown`、`unavailable -> outside_unlock` 不推送）；`numeric_state` / `template` 触发器不走该过滤。订阅时带 `from_state` 的订阅会绕过该守卫。

**disk CLI 形态**（`--variables` 收 JSON；`entity_id`/`condition` 仍是位置参数）：

| 场景 | 命令示例 |
|------|----------|
| 值内含单引号（HA 模板） | `/ha/ha_subscribe sensor.lock_action state_change --description "门锁" --variables "{\"lock_method\": \"{{ states('sensor.lock_method') }}\"}"` |
| 值内无单引号 | `/ha/ha_subscribe sensor.temp above --value 30 --description "客厅过热" --variables '{"site": "front"}'` |

⚠️ **单引号陷阱**：仅当 JSON 值内含单引号时（典型就是 HA 模板 `states('…')`）外层必须用双引号、并把 JSON 内部的 `"` 转义成 `\"`，**不能用单引号包整段 JSON** —— `shlex` 会在下一个单引号处结束该段并静默剥掉模板里的单引号，HA 收到的是 `states(sensor.lock_method)`，不报错但渲染结果不对。值内没有单引号的 JSON 仍可按手册既有示例用单引号。

**手改 `~/.niu/ha-config.json`**：`variables` 必须是对象、每个值必须是标量（字符串/数字/布尔），且不得包含保留键 `trigger`；不满足的条目在建立订阅时被剔除（`ha_subscribe` 侧同样直接拒绝）。

## 6. 测试结果汇总

### 6.1 Phase 1: API 能力摸底

| 测试 | 覆盖范围 | 已验证的 HA 行为特征（以你环境复核为准） |
|------|--------|------|
| REST API | 设备查询/控制/历史等服务面 | 无效服务返回400而非404 |
| WebSocket API | 事件订阅与命令通道 | Config Flow 命令需走 REST（WebSocket 版自 HA 2026.6 起废弃） |
| 事件推送 | 状态变化事件 | 数值状态是"42.0"非"42"，HA浮点行为 |

### 6.2 Phase 2: 事件推送与条件推送

| 测试 | 覆盖范围 | 结论（以你环境复核为准） |
|------|--------|------|
| subscribe_trigger | 触发器注册与触发事件 | trigger 事件识别逻辑已修复，条件过滤可用 |
| 条件推送 | 数值/模板/状态三种触发场景 | 三种场景均可用 |
| 断线重连 | 长连接韧性 | 自动重连+空闲保活可用 |

### 6.3 验证结论

| 验证项 | 结论 |
|--------|------|
| V1 API 能力边界 | REST API 全覆盖设备查询/控制/历史；WebSocket 支持实时事件和条件推送；Config Flow 需用 REST API（非 WebSocket） |
| V2 Agent 替代人工 | 60%+ 设备接入可通过 API 自动化；账号密码型和局域网直连型完全可自动；OAuth 和物理配对需 browser-server 辅助 |
| V3 事件反向推送 | subscribe_trigger 机制成熟，支持精确条件过滤和组合条件，Agent 可靠感知设备状态变化 |
| V4 经验沉淀 | 各品牌接入模式已分类，Config Flow REST API 可查询任意集成的表单结构 |
| V5 真实设备接入 | 已验证完整流程：HACS安装→Xiaomi Miot v1.1.4下载→米家账号集成(需短信验证)→6台真实设备接入→书房灯API开关控制成功 |

## 7. 踩坑记录

### 7.1 Config Flow WebSocket API 废弃

- `config/integration/initialize`, `config/integration/step`, `config/integration/delete` 在 HA 2026.6 返回 `unknown_command`
- 正确方式：REST API `POST /api/config/config_entries/flow`
- 推进流：`POST /api/config/config_entries/flow/{flow_id}`

### 7.2 Trigger 事件格式错误

- 错误假设：`msg["event"]["event_type"] == "trigger"`
- 正确格式：`"trigger" in msg["event"]["variables"]`
- HA 官方文档：trigger 事件通过 `event.variables.trigger` 传递触发信息

### 7.3 Template Trigger 延迟

- `platform: "template"` 的 trigger 响应延迟较大（视你的环境而定）
- 测试 timeout 需放宽设置，组合条件尤甚
- 不像 state/numeric_state trigger 几乎即时

### 7.4 Docker 网络与自动发现

- Docker 默认桥接网络无法发现局域网设备
- `--network=host` 仅 Linux 有效，macOS Docker Desktop 不支持
- 生产环境建议 Linux + host 网络

### 7.5 input_number 浮点行为

- `set_value(42)` → 状态返回 `"42.0"` 而非 `"42"`
- 比较时需注意类型转换或用 `float()` 比较

### 7.6 HA 版本

- 测试版本：2026.6.4
- 不同版本 API 可能有差异，尤其 Config Flow 部分

### 7.7 小米账号短信验证

- Xiaomi Miot 账号集成首次登录会触发 `need_verify` 错误
- 返回验证网页 URL，需在浏览器中打开并输入短信验证码
- 验证链接有时效性（约2-3分钟），必须快速完成
- 验证完成后 Config Flow 自动推进到设备选择步骤
- 这是 Agent 需要用户配合的关键步骤，但只需一次（后续 token 自动续期）

## 8. 真实设备接入验证（Phase 3）

### 8.1 接入的设备

| 设备 | 实体 ID | 类型 | 状态 |
|------|---------|------|------|
| 书房灯 | `light.yeelink_bslamp2_b1ce_light` | light | on（已验证开关控制） |
| 客卧灯 | `light.yeelink_bslamp2_b21a_light` | light | unavailable |
| 门锁 | `sensor.lumi_bmcn03_4cc1_door_state` | sensor | stuck |
| 智能小饭煲 | `sensor.chunmi_eh3_aa6e_cooker` | sensor | unavailable |
| Smart Thermostat | `climate.zinguo_sc01_748f_thermostat` | climate | unavailable |
| 温湿度传感器 | `sensor.miaomiaoce_t1_4d15_temperature` | sensor | unavailable |

### 8.2 真实设备 API 控制验证

**关灯：**
```bash
curl -X POST http://localhost:8123/api/services/light/turn_off \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "light.yeelink_bslamp2_b1ce_light"}'
# 结果：state 从 on → off，灯实际关闭
```

**开灯：**
```bash
curl -X POST http://localhost:8123/api/services/light/turn_on \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"entity_id": "light.yeelink_bslamp2_b1ce_light"}'
# 结果：state 从 off → on，灯实际打开
```

**验证结论：** API → HA → 真实设备的完整链路已打通（端到端延迟以你环境为准）。

### 8.3 设备属性详情

书房灯支持的属性：
- `supported_color_modes`: color_temp, rgb（支持色温和RGB）
- `brightness`: 0-255
- `color_temp_kelvin`: 1700-6500
- `effect_list`: Color, Day

### 8.4 Config Flow 完整步骤记录（真实验证）

```
步骤1: POST /api/config/config_entries/flow → 选择 account 或 token
步骤2: POST /api/config/config_entries/flow/{flow_id} → 提交账密
        → 返回 need_verify + 验证URL
步骤3: 浏览器打开验证URL → 输入短信验证码 → 验证通过
步骤4: POST /api/config/config_entries/flow/{flow_id} → 提交设备过滤（默认全部接入）
        → 返回 create_entry，集成创建成功
```

## 9. ha-server MCP 工具清单

ha-server 提供 8 个 MCP 工具，通过虚拟磁盘 `/ha/` 目录访问：

| 工具 | 类别 | 短描述 | 说明 |
|------|------|--------|------|
| `ha_setup` | 写 | 配置 HA 连接 | 首次使用传入 ha_url/ha_token，无参数返回连接状态 |
| `ha_status` | 读 | 查询设备状态 | 默认返回精简列表（name/area/entity_id/type/state/actions）；传 `entity_id` 返回含 `services`/`properties` 的全量信息；还额外返回 `areas`/`scenes`/`automations` 三类 |
| `ha_control` | 写 | 控制设备 | 立即执行一次操作。优先用 service 参数指定 HA 服务 |
| `ha_subscribe` | 写 | 订阅状态通知 | 条件触发推送。支持 state_change/above/below 三种条件；推送附带实体/触发类型/状态变化/时间/附加上下文（见 5.5）。可选 `variables`（JSON 对象，值须为标量）写入 `trigger.variables`，由 HA 渲染模板后随事件带回 |
| `ha_integrate` | 写 | 管理集成 | 添加/删除设备品牌集成，通过 Config Flow REST API |
| `ha_automation` | 写 | 管理自动化 | 条件触发持续生效的规则 |
| `ha_scene` | 写 | 管理场景 | 多设备瞬间切换到预设状态 |
| `ha_script` | 写 | 管理脚本 | 有序列、有延时的多步骤操作 |

> **返回形状（补录行 / 未加载项）**：`ha_status` 的 `scenes`/`devices` 与 `ha_scene list`/`ha_script list`
> 里，由本地 name→config_key 映射补录出来的行（配置已写入 HA，但实体未加载或已在 HA 内改名）有三个特征：
> `entity_id` 可能为 `null`（场景域解析不到就是 `null`，绝不拿 config_key 拼一个）、`state` 为
> `"unavailable"`、**只有解析不到 `entity_id` 时才带 `note`**（解析到真实 id 的行没有 `note`）。`note`
> 有三种来源：「注册表不可用，无法解析实体 id」/「注册表中无该 config_key 对应的实体（可能未加载或已
> 改名）」/「HA states 不可达，无法解析实体 id」。`ha_status` 与 `ha_scene list` 只会出现前两种；第三种
> 出现在 `ha_scene get`（HTTP 200 命中）、`ha_scene update`（成功返回）与 `ha_scene delete` 的 preview 里，
> 这三处即使调用成功也可能带 `note`。脚本补录行是例外：脚本域豁免，补录的 `script.<config_key>` 就是
> HA 里的真实 entity_id，`state="idle"`，不带 `note`。这类行按 `name` 操作即可，不要把 `entity_id`
> 拼进调用。`ha_scene create`/`snapshot` 返回 `entity_id: null` + `warning` 时同理：配置已持久化，
> 实体尚未加载，稍后用 `ha_scene list` / `activate`（按 name）复查。

### 9.1 工具选择指南

| 需求 | 使用工具 | 说明 |
|------|----------|------|
| 立即执行一次 | `ha_control` | 开关灯、设温度等即时操作 |
| 定时执行一次 | `scheduler` | 定时任务（非 HA 工具） |
| 条件触发持续生效 | `ha_automation` | 如"温度>30°C自动开空调" |
| 多设备瞬间切换 | `ha_scene` | 如"回家模式"同时开灯+开空调 |
| 有序列有延时 | `ha_script` | 如"先开灯→等2秒→调色温" |

### 9.2 相关 Skills

设备控制详细流程见 `ha-device-control` skill，场景/自动化/脚本管理见 `ha-scene-automation` skill（两者均为 `status: active`，已更新至 06-29 版本）。调用 `ha_control`/`ha_scene`/`ha_automation`/`ha_script` 前应先加载对应 skill 获取完整参数和示例。

## 10. HACS 安装（已验证）

### 10.1 HACS 简介

HACS (Home Assistant Community Store) 是 HA 的社区商店，用于安装第三方集成和前端组件。很多品牌的集成（如 Xiaomi Miot Auto）不在 HA 官方内置，必须通过 HACS 安装。

### 10.2 安装步骤

**方法 1：浏览器自动化（Agent 辅助用户）**

1. 打开 HACS 官网下载页面：`https://www.hacs.xyz/docs/use/download/download/`
2. 下载最新的 `hacs.zip`
3. 在 HA 配置目录创建 `custom_components/hacs/` 并解压
4. 重启 HA
5. 在 HA 网页中：设置 → 设备与服务 → 添加集成 → 搜索 "HACS"
6. 按提示完成 GitHub 授权（需要浏览器自动化协助）

**方法 2：命令行安装（Agent 全自动）**

```bash
# 下载 HACS
cd ~/ha-config
mkdir -p custom_components/hacs
wget -q -O hacs.zip https://github.com/hacs/integration/releases/latest/download/hacs.zip
unzip -o hacs.zip -d custom_components/hacs
rm hacs.zip

# 重启 HA
docker restart homeassistant

# 等待 HA 就绪
until curl -s http://localhost:8123/api/ > /dev/null 2>&1; do sleep 5; done
```

**HACS GitHub 授权（需要用户配合）：**

添加 HACS 集成时，需要完成 GitHub Device Authorization：
1. HACS 显示一个设备码和 URL
2. 用户在浏览器中打开 URL，输入设备码，授权
3. Agent 可以通过 browser-server 自动完成此步骤，但需要用户已登录 GitHub

### 10.3 踩坑记录

- HACS 首次安装后，HA 侧边栏不会立即显示 HACS 图标，需要先在"设置 → 设备与服务"中添加 HACS 集成
- GitHub 授权流程需要 VPN（访问 github.com）
- HACS 仓库数据缓存可能需要几分钟更新

## 11. Xiaomi Miot Auto 集成（已验证）

### 11.1 安装

**通过 HACS 下载安装：**

1. 在 HACS 页面搜索 "Xiaomi Miot"
2. 点击进入详情页 → 点击 Download → 确认下载 v1.1.4
3. 下载位置：`/config/custom_components/xiaomi_miot/`
4. 重启 HA 加载新集成

**命令行验证：**

```bash
# 检查文件是否存在
ls ~/ha-config/custom_components/xiaomi_miot/manifest.json

# 通过 Config Flow API 验证集成已加载
curl -s -X POST http://localhost:8123/api/config/config_entries/flow \
  -H "Authorization: Bearer $HA_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"handler": "xiaomi_miot", "show_advanced_options": false, "context": {"source": "user"}}'
```

### 11.2 Config Flow 实测结果

**第一步（选择接入模式）：**

```json
{
  "type": "form",
  "step_id": "user",
  "data_schema": [
    {
      "type": "select",
      "name": "action",
      "required": true,
      "default": "account",
      "options": [
        ["account", "Add devices using Mi Account (账号集成)"],
        ["token", "Add device using host/token (局域网集成)"]
      ]
    }
  ]
}
```

**第二步（账号模式 — 填写账密）：**

推进：`POST /api/config/config_entries/flow/{flow_id}` body: `{"action": "account"}`

```json
{
  "type": "form",
  "step_id": "cloud",
  "data_schema": [
    {"name": "username", "type": "string", "required": true},
    {"name": "password", "type": "string", "required": true},
    {"name": "server_country", "type": "select", "required": true, "default": "cn",
     "options": [["cn","中国大陆"],["tw","中國台灣"],["de","Europe"],["i2","India"],["ru","Russia"],["sg","Singapore"],["us","United States"]]},
    {"name": "conn_mode", "type": "select", "required": true, "default": "auto",
     "options": [["auto","Automatic (自动模式)"],["local","Local (本地模式)"],["cloud","Cloud (云端模式)"]]},
    {"name": "trans_options", "type": "boolean", "required": false, "default": false},
    {"name": "filter_models", "type": "boolean", "required": false, "default": false}
  ]
}
```

**Agent 自动化策略：**
- 必填字段只有 username + password，其余均有默认值
- server_country 默认 "cn"（中国用户无需修改）
- conn_mode 默认 "auto"（自动选择本地/云端，推荐）
- Agent 只需问用户要米家账号密码，即可一键完成接入

**第二步（token 模式 — 局域网直连）：**

推进：`POST /api/config/config_entries/flow/{flow_id}` body: `{"action": "token"}`

（待用户测试时记录字段）

### 11.3 Xiaomi Miot 提供的服务

Xiaomi Miot 安装后，HA 中新增以下服务域：

| 服务 | 说明 |
|------|------|
| `xiaomi_miot.set_property` | 按属性名设置设备属性 |
| `xiaomi_miot.set_miot_property` | 按 siid/piid 设置 MIoT 属性 |
| `xiaomi_miot.get_properties` | 获取设备属性列表 |
| `xiaomi_miot.call_action` | 调用设备动作 |
| `xiaomi_miot.send_command` | 发送 miio 命令 |
| `xiaomi_miot.get_token` | 获取设备 Token |
| `xiaomi_miot.intelligent_speaker` | 小爱同学语音指令 |
| `xiaomi_miot.xiaoai_wakeup` | 唤醒小爱同学 |
| `xiaomi_miot.renew_devices` | 刷新设备列表 |
| `xiaomi_miot.request_xiaomi_api` | 请求小米 API |

### 11.4 连接模式说明

| 模式 | 说明 | 适用设备 |
|------|------|----------|
| **Automatic (自动)** | 支持本地连接的设备用本地，否则走云端 | 推荐，默认 |
| **Local (本地)** | 所有设备仅使用局域网直连 | 仅支持 MIoT-Spec 的 Wi-Fi 设备 |
| **Cloud (云端)** | 所有设备通过小米云端连接 | BLE、ZigBee、miio 设备 |

## 12. Agent 辅助接入的完整流程（验证结论）

### 12.1 首次安装 HA（Agent 协助步骤）

1. **安装 Docker** — `brew install --cask docker`（macOS）
2. **拉取 HA 镜像** — 需 VPN
3. **启动 HA 容器** — `docker run ...`
4. **等待 HA 就绪** — 轮询 `/api/` 端点
5. **首次设置（Onboarding）** — 浏览器自动化辅助
6. **创建 Long-Lived Access Token** — 浏览器自动化辅助

### 12.2 安装 HACS（Agent 协助步骤）

1. **下载 HACS** — 命令行全自动
2. **重启 HA** — 命令行全自动
3. **添加 HACS 集成** — Config Flow API（需 GitHub 授权，可能需 browser-server）

### 12.3 安装品牌集成（Agent 协助步骤）

1. **HACS 下载集成** — 浏览器自动化（搜索 → Download）
2. **重启 HA** — 命令行全自动
3. **添加集成** — Config Flow REST API 全自动
4. **填写账密** — 问用户要（账号密码型）/ 自动发现（局域网型）

### 12.4 设备控制（Agent 全自动）

1. **查询设备列表** — `GET /api/states`
2. **控制设备** — `POST /api/services/{domain}/{service}`
3. **监听状态变化** — WebSocket `subscribe_trigger`
4. **历史查询** — `GET /api/history/period/{timestamp}`

### 12.5 自动化、场景与脚本（Agent 全自动）

**自动化（ha_automation）** — 条件触发持续生效的规则：

| 操作 | 命令示例 |
|------|----------|
| 创建 | `/ha/ha_automation create --name "自动开灯" --config '{"triggers": [{"platform": "state", "entity_id": "light.xxx", "to": "on"}], "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.yyy"}}], "mode": "single"}'` |
| 查看列表 | `/ha/ha_automation list` |
| 查看详情 | `/ha/ha_automation get --name "自动开灯"` |
| 修改 | `/ha/ha_automation update --name "自动开灯" --config '...'` |
| 删除 | `/ha/ha_automation delete --name "自动开灯" --confirm true` |
| 启用/禁用 | `/ha/ha_automation enable --name "自动开灯"` / `disable` |
| 手动触发 | `/ha/ha_automation trigger --name "自动开灯"` |

**trigger 平台**：`state` / `numeric_state` / `time` / `time_pattern` / `sun` / `zone` / `event` / `template` / `mqtt` / `calendar`

**condition 类型**：`state` / `numeric_state` / `time` / `sun` / `zone` / `template` / `and` / `or` / `not`

**action 类型**：服务调用(用 `action` 键) / `delay` / `wait_for_trigger` / `choose` / `if` / `repeat` / `parallel` / `scene` / `stop`

**mode**：`single`（默认，只运行一次）| `restart`（重新开始）| `queued`（排队）| `parallel`（并行）

**场景（ha_scene）** — 多设备瞬间切换到预设状态：

| 操作 | 命令示例 |
|------|----------|
| 创建 | `/ha/ha_scene create --name "回家模式" --config '{"entities": {"light.xxx": {"state": "on", "brightness_pct": 78}}}'` |
| 激活 | `/ha/ha_scene activate --name "回家模式"` |
| 快照 | `/ha/ha_scene snapshot --name "当前状态" --entity-ids '["light.xxx"]'` |
| 查看列表 | `/ha/ha_scene list` |
| 修改 | `/ha/ha_scene update --name "回家模式" --config '...'` |
| 删除 | `/ha/ha_scene delete --name "回家模式" --confirm true` |

**entities 参数名使用 ha_status services 字段中的服务参数名（程序自动转换）**：
- `light`：`brightness_pct` (0-100) / `color_temp_kelvin`
- `climate`：`temperature` / `hvac_mode`
- `switch` / `lock` / `cover`（`position`）/ `fan`（`percentage`）/ `humidifier`（`humidity`）

> **注意**：场景通过 REST Config API 持久化，reload 之后就会出现在 HA states 中——实体 id 由 HA 按
> 名称 slugify 生成（中文名走音译），与 `config_key` 不同源。因此 `ha_status`/`ha_scene list` 只在
> states 缺项时补一行，补录行的 `entity_id` 可能为 `null`、`state="unavailable"`，只有在解析不到 id 时
> 才带 `note`；按 name 操作即可，激活功能不依赖实体已加载（未加载时走 `scene.apply`）。

**脚本（ha_script）** — 有序列、有延时的多步骤操作：

| 操作 | 命令示例 |
|------|----------|
| 创建 | `/ha/ha_script create --name "晚安" --config '{"mode": "single", "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.xxx"}}, {"delay": {"seconds": 2}}, {"action": "light.turn_on", "target": {"entity_id": "light.yyy"}}]}'` |
| 运行 | `/ha/ha_script run --name "晚安"` |
| 查看列表 | `/ha/ha_script list` |
| 修改 | `/ha/ha_script update --name "晚安" --config '...'` |
| 删除 | `/ha/ha_script delete --name "晚安" --confirm true` |

**sequence 动作**：服务调用(`action` 键) / `delay` / `wait_for_trigger` / `choose` / `if` / `repeat` / `parallel` / `condition`

**mode**：`single` | `restart` | `queued` | `parallel`

### 12.6 各步骤的自动化程度

| 步骤 | 自动化程度 | 需要用户配合 |
|------|-----------|-------------|
| 安装 Docker | 0% | 用户自行安装 |
| 启动 HA 容器 | 100% | 无 |
| 首次设置 | 70% | 浏览器自动化辅助 |
| 创建 Token | 80% | 浏览器自动化辅助 |
| 安装 HACS | 80% | GitHub 授权 |
| 安装品牌集成 | 90% | 无（HACS 内下载） |
| 添加账号集成 | 90% | 口述账密 |
| 添加局域网集成 | 95% | 确认设备 |
| 设备控制 | 100% | 无 |
| 自动化/场景/脚本 | 100% | 无 |
| 条件推送 | 100% | 无 |

## 13. 故障排查

### 13.1 配置文件位置

| 文件 | 用途 |
|------|------|
| `~/.niu/ha-config.json` | HA 连接配置（ha_url/ha_token），由 `ha_setup` 写入 |
| `~/.niu/ha-services.json` | HA 服务缓存（`ha_status`/`ha_control` 使用） |

### 13.2 查询连接状态

无参数调用 `ha_setup` 可返回当前 HA 连接状态（URL、是否可达、Token 是否有效）：

```
/ha/ha_setup
```

### 13.3 ha_watcher 日志

`ha_watcher`（`niu_api/internal/ha_watcher/watcher.py`）通过 `print` 输出到 stdout，**无独立日志文件**。如需排查事件推送问题：

- 在 API 进程的 stdout 中查看（启动器会重定向到 `logs/api_stdout.log` 或终端）
- 关注 WebSocket 连接、重连、trigger 事件识别相关输出

### 13.4 常见问题排查路径

| 症状 | 检查项 |
|------|--------|
| `ha_status` 返回空 | 确认 `~/.niu/ha-config.json` 中 URL/Token 有效；确认 HA 已启动且实体未被 `EXCLUDED_DOMAINS` 过滤 |
| `ha_control` 报错 | 用 `ha_status --entity-id <id>` 查看该实体的 `services` 是否包含目标服务 |
| 模拟实体不可见 | `input_*` domain 被 `ha_status` 过滤，只能通过 REST API `/api/states` 访问 |
| 事件推送未收到 | 查 `ha_watcher` stdout 日志；确认 `subscribe_trigger` 订阅成功且条件正确 |

### 13.5 存量自动化引用了不存在的 `scene.<uuid>` 时如何核查

修复前的 `ha_scene create`/`get`/`update`/`delete` 会把 `config_key` 拼成 `scene.<uuid>` 返回（该实体
在 HA 里并不存在）。若把它写进自动化或脚本，触发时会报 `Unable to find entity` / 日志里出现
`Entity ID scene.<uuid> is not found`。本次修复只纠正后续返回值，**不会**自动扫描改写已有自动化
（spec §2 非目标），按下面步骤手工核查：

1. 先取真实场景实体清单：`/ha/ha_scene list`（或 `GET /api/states` 只留 `scene.` 开头的 entity_id）。
   补录行的 `entity_id` 可能是 `null`，此时记下 `name`，别把 `null` 当引用。
2. 逐条拉出自动化配置：`/ha/ha_automation list --detail true`（`detail=true` 直接带 `config`）。
3. 在每条 `config` 里搜 `scene.` 引用，覆盖这几处：
   - `actions[].action` 为 `scene.turn_on` / `scene.activate` 时，`target.entity_id`、
     `target.entity_id` 列表、`data.entity_id`
   - 旧式 `actions[].action == "scene"`（或 `scene.turn_on` 写在 `service_data` 里）
   - `conditions[].condition == "state"` 且 `entity_id` 以 `scene.` 开头
   - 同一份配置里的 `script.<config_key>` 引用无需核查：脚本域豁免，`script.<config_key>` 就是 HA 里
     真实的 entity_id（只有场景 id 与 config_key 不同源），不存在这类幽灵 id
4. 逐个判定：第 3 步收集到的 id 若不在第 1 步的真实清单里 ⇒ 该引用无效。特征判别：
   `scene.` 后面是 32 位十六进制（uuid hex）基本可判定是修复前拼装的幽灵 id；真实 id 是名称 slug
   （ASCII 名 → 小写下划线；中文名 → 音译 slug）。
5. 修正：`/ha/ha_automation update --name "<规则名>" --config '...'` 重写该条，把 target 换成真实
   entity_id；随后 `/ha/ha_automation trigger --name "<规则名>"` 触发一次，并在 HA「开发者工具 →
   操作 → 检查配置」与 HA 日志里确认没有 `is not found`。
6. 顺带清理残留映射：`ha_scene list` 里 `state="unavailable"` 且带 `note` 的行，说明该行没解析出实体 id —
   可能是场景未加载或已在 HA 内改名，也可能只是注册表取数不可用，不能据此断定 HA 里已不存在。先按第 1 步
   的真实清单核对，确认不需要后再用 `/ha/ha_scene delete --name "<场景名>" --confirm true`
   （HA 侧已 404 时，只有映射值仍指向该 config_key 才会清掉本地映射，否则保留）。

