# 智能家居集成验证测试方案

> 日期: 2026-06-22
> 状态: 设计阶段
> 核心命题: 验证 Home Assistant 的 API 能力是否足够让 AI Agent 替代人工完成智能家居的发现、接入、配置、控制和事件订阅，使得任何普通用户都可以零门槛搭建智能家居管控中心。

---

## 1. 测试目标

| 编号 | 验证项 | 成功标准 |
|------|--------|----------|
| V1 | HA API 能力边界 | 完整列出 REST/WebSocket/Config Flow 三类 API 的能力清单，明确标注"能做"和"不能做"，不能做的给出替代方案或结论 |
| V2 | Agent 替代人工操作 | Agent 能通过 API 完成：设备发现(列出集成/设备/实体)、配置流推进(发起/选择/填写/完成)、状态读取(get_state)、设备控制(call_service)，全程无需用户打开 HA Web UI |
| V3 | 事件反向推送 | WebSocket 长连接能稳定订阅 `state_changed` 事件，模拟实体状态变更后脚本在 2 秒内收到事件并打印完整 payload；`subscribe_trigger` 能精确过滤（如"input_boolean 从 off 变 on"） |
| V4 | 经验沉淀可行性 | 每类 API 的行为模式可归纳为固定步骤序列，常见配置流的字段和选项可枚举，至少 3 个操作流程可写成 Skill 模板 |
| V5 | 真实设备接入 | 小米灯通过 Xiaomi Miot Auto 集成完成从"安装集成"到"灯光控制"的全流程，Agent 能引导用户完成每一步，最终通过 API 控制灯光开关和亮度 |

---

## 2. 测试环境搭建

### 2.1 Docker 安装 Home Assistant

```bash
# 创建数据目录
mkdir -p ~/ha-config

# 启动 HA 容器
docker run -d \
  --name homeassistant \
  --privileged \
  --restart=unless-stopped \
  -e TZ=Asia/Shanghai \
  -v ~/ha-config:/config \
  -v /run/dbus:/run/dbus:ro \
  --network=host \
  ghcr.io/home-assistant/home-assistant:stable

# 等待启动完成（约 2-3 分钟）
docker logs -f homeassistant
# 看到 "Home Assistant is running" 即可

# Mac 上 --network=host 不生效，改用端口映射：
docker run -d \
  --name homeassistant \
  --restart=unless-stopped \
  -e TZ=Asia/Shanghai \
  -v ~/ha-config:/config \
  -p 8123:8123 \
  ghcr.io/home-assistant/home-assistant:stable
```

首次访问 `http://localhost:8123`，完成 Onboarding（创建用户名密码）。

### 2.2 创建 Long-Lived Access Token

1. 登录 HA Web UI → 左下角用户头像 → Security → Long-Lived Access Tokens
2. 点击 "Create Token"，名称填 `agent-test`
3. 复制 Token 保存到环境变量：

```bash
export HA_URL="http://localhost:8123"
export HA_TOKEN="eyJhbGciOi..."  # 粘贴实际 Token
```

验证 Token 可用：

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  "$HA_URL/api/" | python3 -m json.tool
# 预期返回: {"message": "API running.", "message_version": "2024.x"}
```

### 2.3 创建模拟实体（虚拟设备）

在 `~/ha-config/configuration.yaml` 末尾追加：

```yaml
# 模拟开关（测试 on/off 控制）
input_boolean:
  virtual_light:
    name: "虚拟灯"
    icon: mdi:lightbulb
  virtual_fan:
    name: "虚拟风扇"
    icon: mdi:fan

# 模拟数值（测试亮度/温度调节）
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

# 模拟选择（测试下拉选项）
input_select:
  virtual_mode:
    name: "虚拟模式"
    options:
      - "舒适"
      - "节能"
      - "睡眠"
    icon: mdi:format-list-bulleted

# 模拟按钮（测试一次性动作）
input_button:
  virtual_reset:
    name: "虚拟重置"
    icon: mdi:refresh
```

重载配置：

```bash
curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" \
  "$HA_URL/api/services/homeassistant/reload_all"
```

或在 HA Web UI → 开发者工具 → YAML → 点击 "Reload All"。

验证实体已创建：

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
  "$HA_URL/api/states/input_boolean.virtual_light" | python3 -m json.tool
# 预期返回包含 entity_id, state("off"), attributes 等
```

---

## 3. 测试方案

### Phase 1: API 能力摸底（纯脚本，不接 LLM）

目标：用 Python 脚本逐一测试 HA 的 REST/WebSocket/Config Flow API，记录每个端点的行为和限制。

#### 3.1.1 REST API 测试

**基础信息类**

```python
import requests

headers = {"Authorization": f"Bearer {HA_TOKEN}"}

# 1. API 健康检查
resp = requests.get(f"{HA_URL}/api/", headers=headers)
# 预期: {"message": "API running."}

# 2. 获取 HA 配置信息（版本、时区、单位等）
resp = requests.get(f"{HA_URL}/api/config", headers=headers)
# 预期: {"version": "2024.x", "timezone": "Asia/Shanghai", ...}

# 3. 获取所有实体状态
resp = requests.get(f"{HA_URL}/api/states", headers=headers)
# 预期: 返回所有实体列表，每个包含 entity_id, state, attributes, last_changed

# 4. 获取单个实体状态
resp = requests.get(f"{HA_URL}/api/states/input_boolean.virtual_light", headers=headers)
# 预期: {"entity_id": "input_boolean.virtual_light", "state": "off", ...}
```

**设备控制类**

```python
# 5. 调用服务 — 开灯
resp = requests.post(f"{HA_URL}/api/services/input_boolean/turn_on", headers=headers,
    json={"entity_id": "input_boolean.virtual_light"})
# 预期: 返回被影响的实体列表

# 6. 调用服务 — 关灯
resp = requests.post(f"{HA_URL}/api/services/input_boolean/turn_off", headers=headers,
    json={"entity_id": "input_boolean.virtual_light"})

# 7. 调用服务 — 切换
resp = requests.post(f"{HA_URL}/api/services/input_boolean/toggle", headers=headers,
    json={"entity_id": "input_boolean.virtual_light"})

# 8. 调用服务 — 设置亮度
resp = requests.post(f"{HA_URL}/api/services/input_number/set_value", headers=headers,
    json={"entity_id": "input_number.virtual_brightness", "value": 75})

# 9. 调用服务 — 设置模式
resp = requests.post(f"{HA_URL}/api/services/input_select/select_option", headers=headers,
    json={"entity_id": "input_select.virtual_mode", "option": "节能"})

# 10. 调用服务 — 按按钮
resp = requests.post(f"{HA_URL}/api/services/input_button/press", headers=headers,
    json={"entity_id": "input_button.virtual_reset"})
```

**历史查询类**

```python
from datetime import datetime, timedelta

# 11. 获取实体历史（最近 1 小时）
end_time = datetime.utcnow().isoformat()
start_time = (datetime.utcnow() - timedelta(hours=1)).isoformat()
resp = requests.get(
    f"{HA_URL}/api/history/period/{start_time}",
    headers=headers,
    params={"filter_entity_id": "input_boolean.virtual_light", "end_time": end_time}
)
# 预期: 返回状态变更时间线 [[{"state": "off", "last_changed": "..."}, ...]]

# 12. 获取实体最近状态变更
resp = requests.get(
    f"{HA_URL}/api/history/period",
    headers=headers,
    params={"filter_entity_id": "input_boolean.virtual_light"}
)

# 13. 搜索事件日志
resp = requests.get(
    f"{HA_URL}/api/logbook",
    headers=headers,
    params={"entity": "input_boolean.virtual_light", "start_time": start_time}
)
```

**集成与设备发现类**

```python
# 14. 列出所有集成
resp = requests.get(f"{HA_URL}/api/config/integrations", headers=headers)
# 注意: 此端点可能不存在于 REST API，需确认

# 15. 列出所有设备（通过 WebSocket 更可靠，见 3.1.2）
# REST API 没有直接的 /api/devices 端点

# 16. 列出所有实体域（domain）
resp = requests.get(f"{HA_URL}/api/states", headers=headers)
domains = set(e["entity_id"].split(".")[0] for e in resp.json())
# 预期: {'input_boolean', 'input_number', 'input_select', 'input_button', 'sun', ...}
```

**关键发现记录**：每个测试用例记录：
- HTTP 状态码
- 返回数据结构（关键字段）
- 是否符合预期
- 限制或异常（如端点不存在、权限不足、数据格式不符）

#### 3.1.2 WebSocket API 测试

WebSocket 是 HA 的核心实时通信通道，支持订阅和长连接。

```python
import asyncio
import json
import websockets

HA_WS_URL = f"ws://localhost:8123/api/websocket"

async def ws_test():
    async with websockets.connect(HA_WS_URL) as ws:
        # 1. 认证
        result = json.loads(await ws.recv())
        # 预期: {"type": "auth_required", "ha_version": "2024.x"}

        await ws.send(json.dumps({
            "type": "auth",
            "access_token": HA_TOKEN
        }))
        result = json.loads(await ws.recv())
        # 预期: {"type": "auth_ok", "ha_version": "2024.x"}

        # 2. 获取所有实体状态（等价于 REST GET /api/states）
        await ws.send(json.dumps({
            "id": 1,
            "type": "get_states"
        }))
        result = json.loads(await ws.recv())
        # 预期: {"id": 1, "type": "result", "success": true, "result": [...]}

        # 3. 获取所有服务
        await ws.send(json.dumps({
            "id": 2,
            "type": "get_services"
        }))
        result = json.loads(await ws.recv())
        # 预期: 返回所有域及其服务定义

        # 4. 调用服务（等价于 REST POST /api/services/...）
        await ws.send(json.dumps({
            "id": 3,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_on",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        result = json.loads(await ws.recv())
        # 预期: {"id": 3, "type": "result", "success": true}

        # 5. 订阅 state_changed 事件
        await ws.send(json.dumps({
            "id": 4,
            "type": "subscribe_events",
            "event_type": "state_changed"
        }))
        result = json.loads(await ws.recv())
        # 预期: {"id": 4, "type": "result", "success": true}
        # 之后每次状态变更都会收到 event 消息

        # 6. 触发状态变更并接收事件
        await ws.send(json.dumps({
            "id": 5,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_off",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        # 预期收到两条消息:
        # a) call_service 的 result
        # b) state_changed 的 event

        # 7. subscribe_trigger — 精确过滤
        await ws.send(json.dumps({
            "id": 6,
            "type": "subscribe_trigger",
            "trigger": {
                "platform": "state",
                "entity_id": "input_boolean.virtual_light",
                "from": "off",
                "to": "on"
            }
        }))
        result = json.loads(await ws.recv())
        # 预期: {"id": 6, "type": "result", "success": true}
        # 只有 virtual_light 从 off 变 on 时才触发

        # 8. 测试 trigger 过滤 — off→off 不触发
        await ws.send(json.dumps({
            "id": 7,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_off",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        # 预期: 不收到 trigger event（因为已经是 off）

        # 9. 测试 trigger 过滤 — off→on 触发
        await ws.send(json.dumps({
            "id": 8,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_on",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        # 预期: 收到 trigger event

        # 10. 获取设备列表
        await ws.send(json.dumps({
            "id": 9,
            "type": "config/device_registry/list"
        }))
        result = json.loads(await ws.recv())
        # 预期: 返回所有已注册设备列表

        # 11. 获取区域列表
        await ws.send(json.dumps({
            "id": 10,
            "type": "config/area_registry/list"
        }))
        result = json.loads(await ws.recv())
        # 预期: 返回所有区域

        # 12. 取消订阅
        await ws.send(json.dumps({
            "id": 11,
            "type": "unsubscribe_events",
            "subscription": 4  # 对应 subscribe_events 的 id
        }))

asyncio.run(ws_test())
```

#### 3.1.3 Config Flow API 测试

Config Flow 是 HA 程序化添加集成的关键 API，决定 Agent 能否自动完成设备接入。

```python
async def config_flow_test():
    async with websockets.connect(HA_WS_URL) as ws:
        # 认证（同上，省略）

        # 1. 列出可用的集成域
        await ws.send(json.dumps({
            "id": 20,
            "type": "config/integration/list"
        }))
        result = json.loads(await ws.recv())
        # 预期: 返回所有已安装集成的列表

        # 2. 发起配置流 — 以 Demo 集成为例
        await ws.send(json.dumps({
            "id": 21,
            "type": "config/integration/initialize",
            "domain": "demo"  # demo 集成无需真实设备
        }))
        result = json.loads(await ws.recv())
        # 预期: 返回 flow_id 和第一步的 schema
        # {"type": "form", "flow_id": "xxx", "step_id": "user",
        #  "data_schema": {...}, "errors": null}

        # 3. 提交配置流第一步
        flow_id = result["result"]["flow_id"]
        await ws.send(json.dumps({
            "id": 22,
            "type": "config/integration/step",
            "flow_id": flow_id,
            "step_id": "user",
            "user_input": {}  # demo 集成第一步通常无需输入
        }))
        result = json.loads(await ws.recv())
        # 预期: {"type": "create_entry", ...} 表示配置完成

        # 4. 测试不存在的集成
        await ws.send(json.dumps({
            "id": 23,
            "type": "config/integration/initialize",
            "domain": "nonexistent_integration"
        }))
        result = json.loads(await ws.recv())
        # 预期: 返回错误

        # 5. 列出正在进行的配置流
        await ws.send(json.dumps({
            "id": 24,
            "handler": "config/integration",
            "type": "list"
        }))

        # 6. 删除集成条目
        await ws.send(json.dumps({
            "id": 25,
            "type": "config/integration/delete",
            "entry_id": "..."  # 从 create_entry 结果获取
        }))
```

**Config Flow 关键问题清单**：

| 问题 | 验证方法 | 影响 |
|------|----------|------|
| 能否程序化发起任意集成的配置流？ | 测试多个集成域 | Agent 能否自动发现并接入 |
| 配置流的 schema 是否可解析？ | 检查 data_schema 结构 | Agent 能否自动填写表单 |
| 多步配置流如何推进？ | 测试需要多步的集成 | Agent 能否处理复杂流程 |
| 外部认证（OAuth/扫码）如何处理？ | 测试需要 OAuth 的集成 | 哪些集成需要人工介入 |
| 配置流能否中途取消？ | 测试 abort 操作 | Agent 能否回滚错误操作 |

---

### Phase 2: 事件推送验证

目标：验证 WebSocket 长连接能稳定接收 HA 事件，为 Agent 被动感知设备状态变化奠定基础。

#### 3.2.1 WebSocket 长连接守护线程

```python
import threading
import time
import json
import websockets
import asyncio
from datetime import datetime

class HAEventWatcher:
    """HA WebSocket 事件守护线程 — 模拟未来 MCP Server 的事件推送机制"""

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.ws = None
        self._msg_id = 0
        self._loop = None
        self._thread = None
        self._running = False
        self._callbacks = {}  # event_type -> callback

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def subscribe(self, event_type, callback):
        """注册事件回调"""
        self._callbacks[event_type] = callback

    def _next_id(self):
        self._msg_id += 1
        return self._msg_id

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        while self._running:
            try:
                self._loop.run_until_complete(self._connect_and_listen())
            except Exception as e:
                print(f"[Watcher] 连接断开: {e}, 5秒后重连...")
                time.sleep(5)

    async def _connect_and_listen(self):
        async with websockets.connect(self.url) as ws:
            self.ws = ws
            print("[Watcher] WebSocket 已连接")

            # 认证
            msg = json.loads(await ws.recv())
            await ws.send(json.dumps({
                "type": "auth",
                "access_token": self.token
            }))
            msg = json.loads(await ws.recv())
            if msg.get("type") != "auth_ok":
                raise Exception(f"认证失败: {msg}")

            print("[Watcher] 认证成功")

            # 订阅 state_changed
            await ws.send(json.dumps({
                "id": self._next_id(),
                "type": "subscribe_events",
                "event_type": "state_changed"
            }))
            print("[Watcher] 已订阅 state_changed")

            # 持续监听
            while self._running:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if msg.get("type") == "event":
                    event_data = msg.get("event", {})
                    event_type = event_data.get("event_type", "")
                    if event_type in self._callbacks:
                        self._callbacks[event_type](event_data)
                    else:
                        # 默认打印
                        ts = datetime.now().strftime("%H:%M:%S")
                        entity = event_data.get("data", {}).get("entity_id", "?")
                        new_state = event_data.get("data", {}).get("new_state", {})
                        state_val = new_state.get("state", "?") if new_state else "removed"
                        print(f"[{ts}] {entity} → {state_val}")

                # 发送 ping 保活
                if msg.get("type") == "pong":
                    pass

            # 定期 ping
            # （实际实现中需要 ping/pong 保活机制）
```

#### 3.2.2 事件接收验证脚本

```python
# test_event_push.py

def on_state_changed(event_data):
    """state_changed 事件回调"""
    data = event_data.get("data", {})
    entity_id = data.get("entity_id")
    old_state = data.get("old_state", {})
    new_state = data.get("new_state", {})

    old_val = old_state.get("state") if old_state else "None"
    new_val = new_state.get("state") if new_state else "None"

    print(f"  实体: {entity_id}")
    print(f"  旧值: {old_val}")
    print(f"  新值: {new_val}")
    print(f"  属性: {new_state.get('attributes', {})}")
    print(f"  变更时间: {new_state.get('last_changed')}")
    print("---")

# 启动守护线程
watcher = HAEventWatcher(
    url="ws://localhost:8123/api/websocket",
    token=HA_TOKEN
)
watcher.subscribe("state_changed", on_state_changed)
watcher.start()

# 等待连接建立
time.sleep(2)

# 触发状态变更
import requests
headers = {"Authorization": f"Bearer {HA_TOKEN}"}

print("=== 测试 1: 开灯 ===")
requests.post(f"{HA_URL}/api/services/input_boolean/turn_on", headers=headers,
    json={"entity_id": "input_boolean.virtual_light"})
time.sleep(1)

print("=== 测试 2: 关灯 ===")
requests.post(f"{HA_URL}/api/services/input_boolean/turn_off", headers=headers,
    json={"entity_id": "input_boolean.virtual_light"})
time.sleep(1)

print("=== 测试 3: 调亮度 ===")
requests.post(f"{HA_URL}/api/services/input_number/set_value", headers=headers,
    json={"entity_id": "input_number.virtual_brightness", "value": 42})
time.sleep(1)

print("=== 测试 4: 切换模式 ===")
requests.post(f"{HA_URL}/api/services/input_select/select_option", headers=headers,
    json={"entity_id": "input_select.virtual_mode", "option": "睡眠"})
time.sleep(1)

watcher.stop()
```

#### 3.2.3 subscribe_trigger 精确过滤测试

```python
async def trigger_filter_test():
    """测试 subscribe_trigger 的精确过滤能力"""
    async with websockets.connect(HA_WS_URL) as ws:
        # 认证（省略）

        triggered_events = []

        # 订阅精确触发器：virtual_light 从 off 变 on
        await ws.send(json.dumps({
            "id": 30,
            "type": "subscribe_trigger",
            "trigger": {
                "platform": "state",
                "entity_id": "input_boolean.virtual_light",
                "from": "off",
                "to": "on"
            }
        }))
        result = json.loads(await ws.recv())
        assert result["success"], f"subscribe_trigger 失败: {result}"

        # 测试 A: off → on（应触发）
        await ws.send(json.dumps({
            "id": 31,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_on",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        await asyncio.sleep(0.5)
        # 收集消息，检查是否收到 trigger event

        # 测试 B: on → on（不应触发）
        await ws.send(json.dumps({
            "id": 32,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_on",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        await asyncio.sleep(0.5)

        # 测试 C: on → off（不应触发，因为 trigger 要求 off→on）
        await ws.send(json.dumps({
            "id": 33,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_off",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        await asyncio.sleep(0.5)

        # 测试 D: off → on（应再次触发）
        await ws.send(json.dumps({
            "id": 34,
            "type": "call_service",
            "domain": "input_boolean",
            "service": "turn_on",
            "service_data": {"entity_id": "input_boolean.virtual_light"}
        }))
        await asyncio.sleep(0.5)

        # 验证: 只有测试 A 和 D 触发了 trigger event

        # 测试 E: 数值范围触发器
        await ws.send(json.dumps({
            "id": 35,
            "type": "subscribe_trigger",
            "trigger": {
                "platform": "numeric_state",
                "entity_id": "input_number.virtual_brightness",
                "above": 80
            }
        }))

        # 设置亮度为 90（应触发）
        await ws.send(json.dumps({
            "id": 36,
            "type": "call_service",
            "domain": "input_number",
            "service": "set_value",
            "service_data": {
                "entity_id": "input_number.virtual_brightness",
                "value": 90
            }
        }))

        # 设置亮度为 50（不应触发，因为从 90 降到 50 不满足 above 80）
        await ws.send(json.dumps({
            "id": 37,
            "type": "call_service",
            "domain": "input_number",
            "service": "set_value",
            "service_data": {
                "entity_id": "input_number.virtual_brightness",
                "value": 50
            }
        }))
```

---

### Phase 3: 真实设备接入（小米灯）

目标：验证真实设备从安装集成到控制的全流程，评估 Agent 引导式配置的可行性。

#### 3.3.1 安装 HACS（Home Assistant Community Store）

HACS 是安装第三方集成的必要前置。

```bash
# 方法 1: 官方安装脚本（推荐）
docker exec homeassistant bash -c "
  wget -O - https://get.hacs.xyz | bash -
"

# 方法 2: 手动安装
docker exec homeassistant bash -c "
  mkdir -p /config/custom_components/hacs
  cd /config/custom_components/hacs
  wget https://github.com/hacs/integration/releases/latest/download/hacs.zip
  unzip hacs.zip
  rm hacs.zip
"

# 重启 HA
docker restart homeassistant

# 等待重启完成后，在 HA Web UI:
# 1. 设置 → 集成 → 添加集成 → 搜索 "HACS"
# 2. 按提示完成 GitHub 授权
```

**Agent 可行性评估点**：
- HACS 安装可通过 Docker exec 命令完成 — 可程序化
- GitHub 授权需要浏览器 OAuth — 需要人工介入
- 结论：HACS 安装的前半段可程序化，OAuth 步骤需引导用户

#### 3.3.2 安装 Xiaomi Miot Auto

```bash
# 通过 HACS 安装（推荐）
# HA Web UI → HACS → 集成 → 搜索 "Xiaomi Miot Auto" → 安装

# 或手动安装
docker exec homeassistant bash -c "
  cd /config/custom_components
  git clone https://github.com/al-one/hass-xiaomi-miot.git xiaomi_miot
"

# 重启 HA
docker restart homeassistant
```

#### 3.3.3 米家账号配置流

```python
async def xiaomi_config_flow_test():
    """测试小米集成的配置流 — 评估 Agent 能否程序化完成"""
    async with websockets.connect(HA_WS_URL) as ws:
        # 认证（省略）

        # 1. 发起 Xiaomi Miot Auto 配置流
        await ws.send(json.dumps({
            "id": 40,
            "type": "config/integration/initialize",
            "domain": "xiaomi_miot"
        }))
        result = json.loads(await ws.recv())

        # 分析返回的 schema:
        # - 是否包含 username/password 字段？
        # - 是否有选择项（连接方式：云端/局域网）？
        # - 字段是否有描述和验证规则？

        if result.get("result", {}).get("type") == "form":
            step_id = result["result"]["step_id"]
            schema = result["result"].get("data_schema", {})
            print(f"配置流第一步: {step_id}")
            print(f"Schema: {json.dumps(schema, indent=2, ensure_ascii=False)}")

            # 2. 提交米家账号
            await ws.send(json.dumps({
                "id": 41,
                "type": "config/integration/step",
                "flow_id": result["result"]["flow_id"],
                "step_id": step_id,
                "user_input": {
                    "username": "your_xiaomi_account@example.com",
                    "password": "your_password",
                    # 其他字段根据 schema 填写
                }
            }))
            result = json.loads(await ws.recv())

            # 3. 分析后续步骤
            # 可能的结果:
            # - create_entry: 配置成功，设备自动发现
            # - form: 需要更多输入（如选择设备、选择连接方式）
            # - external: 需要外部认证（OAuth）
            # - abort: 配置中止（如账号错误）

            print(f"配置流结果类型: {result.get('result', {}).get('type')}")
```

**配置流步骤记录模板**：

```
集成: xiaomi_miot
步骤 1:
  - step_id: user
  - 字段: username(文本), password(密码), server(选择: cn/i2/sg/us/de)
  - Agent 可填写: username, password, server
  - 需要用户输入: 米家账号密码
步骤 2:
  - step_id: ...
  - 字段: ...
  - Agent 可填写: ...
  - 需要用户输入: ...
最终结果:
  - 类型: create_entry / external / abort
  - 自动发现设备数: N
```

#### 3.3.4 真实灯光控制

```python
# 配置流完成后，小米灯会自动出现在 HA 中
# 实体 ID 格式: light.xiaomi_light_xxxx

# 1. 查找小米灯实体
resp = requests.get(f"{HA_URL}/api/states", headers=headers)
xiaomi_lights = [e for e in resp.json() if e["entity_id"].startswith("light.")]
print(f"发现灯光: {[e['entity_id'] for e in xiaomi_lights]}")

# 2. 开灯
resp = requests.post(f"{HA_URL}/api/services/light/turn_on", headers=headers,
    json={"entity_id": "light.xiaomi_light_xxxx"})

# 3. 关灯
resp = requests.post(f"{HA_URL}/api/services/light/turn_off", headers=headers,
    json={"entity_id": "light.xiaomi_light_xxxx"})

# 4. 调亮度
resp = requests.post(f"{HA_URL}/api/services/light/turn_on", headers=headers,
    json={"entity_id": "light.xiaomi_light_xxxx", "brightness_pct": 50})

# 5. 调色温
resp = requests.post(f"{HA_URL}/api/services/light/turn_on", headers=headers,
    json={"entity_id": "light.xiaomi_light_xxxx", "kelvin": 4000})

# 6. 调颜色
resp = requests.post(f"{HA_URL}/api/services/light/turn_on", headers=headers,
    json={"entity_id": "light.xiaomi_light_xxxx", "rgb_color": [255, 128, 0]})
```

#### 3.3.5 其他设备接入评估

| 设备 | HA 集成 | 接入方式 | Agent 可行性 |
|------|---------|----------|-------------|
| 小米灯 | Xiaomi Miot Auto | 米家账号配置流 | 高 — 账号密码可程序化填写 |
| 美的空调 | Midea AC LAN / Xiaomi Miot | 局域网发现或米家账号 | 中 — 局域网发现可能需手动确认 |
| 格力空调 | Gree Climate / Xiaomi Miot | 局域网或米家账号 | 中 — 同上 |
| 科沃斯扫地机 | Ecovacs / Xiaomi Miot | 账号配置流 | 中 — Ecovacs 需要 OAuth |
| 小米 BLE 设备 | Xiaomi BLE + 蓝牙桥 | 蓝牙配对 | 低 — 需要物理操作（按配对键） |

---

### Phase 4: 经验总结

#### 3.4.1 API 行为模式总结

每个 API 端点按以下模板记录：

```
端点: POST /api/services/{domain}/{service}
用途: 调用 HA 服务
请求体: {"entity_id": "...", ...service_data}
响应: 被影响的实体列表
限制:
  - entity_id 必须存在，否则静默失败（不报错）
  - 某些服务需要特定域的实体
  - 批量操作用列表: {"entity_id": ["light.a", "light.b"]}
常见错误:
  - 401: Token 无效或过期
  - 404: 服务不存在
  - 400: 参数格式错误
```

#### 3.4.2 常见配置流步骤总结

```
模式 1: 账号密码型（如 Xiaomi Miot）
  步骤: initialize → 填写账号密码 → create_entry
  Agent 可自动化: 90%（只需用户提供账号密码）

模式 2: 局域网发现型（如 ESPHome）
  步骤: initialize → 自动发现设备 → 选择设备 → create_entry
  Agent 可自动化: 80%（发现自动，选择需确认）

模式 3: OAuth 认证型（如 Ecovacs、Google）
  步骤: initialize → external(OAuth URL) → 用户授权 → callback → create_entry
  Agent 可自动化: 30%（OAuth 必须用户浏览器操作）

模式 4: 手动配对型（如蓝牙设备）
  步骤: initialize → 等待配对 → 用户按按钮 → create_entry
  Agent 可自动化: 10%（需要物理操作）
```

#### 3.4.3 Skill 可行性评估

**可写成 Skill 的操作**：

1. **HA 环境初始化 Skill**
   - 输入: Docker 路径、端口、时区
   - 步骤: docker run → 等待启动 → 创建 Token → 验证
   - 可程序化: 100%

2. **设备状态查询 Skill**
   - 输入: 实体 ID（或域/关键词）
   - 步骤: GET /api/states → 过滤 → 格式化输出
   - 可程序化: 100%

3. **设备控制 Skill**
   - 输入: 实体 ID + 服务 + 参数
   - 步骤: POST /api/services/{domain}/{service} → 验证结果
   - 可程序化: 100%

4. **账号密码型集成接入 Skill**
   - 输入: 集成域名 + 账号 + 密码 + 选项
   - 步骤: initialize → 填写 → 推进 → 验证
   - 可程序化: 90%（需用户提供账号密码）

5. **事件订阅 Skill**
   - 输入: 事件类型 / 触发条件
   - 步骤: WebSocket 连接 → subscribe → 守护线程
   - 可程序化: 100%

**不能写成 Skill 的操作**：

1. **OAuth 认证流程** — 需要用户在浏览器中操作
2. **蓝牙设备配对** — 需要用户按物理按钮
3. **网络问题排查** — 需要现场判断（IP 冲突、防火墙、子网隔离）
4. **特定硬件型号适配** — 不同型号的配置参数差异大，无法通用化

---

## 4. 测试项目

### 4.1 Phase 1: API 能力摸底

| 编号 | 验证项 | 操作 | 预期结果 | 实际结果 |
|------|--------|------|----------|----------|
| T1.1 | V1 | `GET /api/` 健康检查 | 返回 `{"message": "API running."}` | |
| T1.2 | V1 | `GET /api/config` 获取配置 | 返回版本、时区、单位等信息 | |
| T1.3 | V1 | `GET /api/states` 获取所有实体 | 返回实体列表，包含模拟实体 | |
| T1.4 | V1 | `GET /api/states/{entity_id}` 获取单个实体 | 返回指定实体的完整状态 | |
| T1.5 | V1 | `GET /api/states/nonexistent` 查询不存在的实体 | 返回 404 | |
| T1.6 | V2 | `POST /api/services/input_boolean/turn_on` 开灯 | 实体状态变为 on | |
| T1.7 | V2 | `POST /api/services/input_boolean/turn_off` 关灯 | 实体状态变为 off | |
| T1.8 | V2 | `POST /api/services/input_boolean/toggle` 切换 | 状态翻转 | |
| T1.9 | V2 | `POST /api/services/input_number/set_value` 设亮度 | 数值变为指定值 | |
| T1.10 | V2 | `POST /api/services/input_select/select_option` 选模式 | 选项变为指定值 | |
| T1.11 | V2 | `POST /api/services/input_button/press` 按按钮 | 按钮触发，last_changed 更新 | |
| T1.12 | V1 | `GET /api/history/period` 查询历史 | 返回状态变更时间线 | |
| T1.13 | V1 | `GET /api/logbook` 查询日志 | 返回事件日志 | |
| T1.14 | V1 | WebSocket `get_states` | 等价于 REST GET /api/states | |
| T1.15 | V1 | WebSocket `get_services` | 返回所有域的服务定义 | |
| T1.16 | V2 | WebSocket `call_service` | 等价于 REST 调用服务 | |
| T1.17 | V1 | WebSocket `config/device_registry/list` | 返回设备列表 | |
| T1.18 | V1 | WebSocket `config/area_registry/list` | 返回区域列表 | |
| T1.19 | V1 | Config Flow `initialize` (demo) | 返回 flow_id 和 schema | |
| T1.20 | V1 | Config Flow `step` 提交表单 | 返回 create_entry 或下一步 form | |
| T1.21 | V1 | Config Flow `initialize` (不存在集成) | 返回错误 | |
| T1.22 | V1 | Config Flow `delete` 删除集成 | 集成被移除 | |

### 4.2 Phase 2: 事件推送验证

| 编号 | 验证项 | 操作 | 预期结果 | 实际结果 |
|------|--------|------|----------|----------|
| T2.1 | V3 | WebSocket 连接 + 认证 | 成功建立连接并认证 | |
| T2.2 | V3 | `subscribe_events` state_changed | 订阅成功，返回 success:true | |
| T2.3 | V3 | 开灯 → 接收 state_changed 事件 | 2 秒内收到事件，包含 entity_id、新旧状态 | |
| T2.4 | V3 | 关灯 → 接收 state_changed 事件 | 同上，新状态为 off | |
| T2.5 | V3 | 调亮度 → 接收 state_changed 事件 | 事件包含 attributes.brightness 变化 | |
| T2.6 | V3 | `subscribe_trigger` off→on 过滤 | 只有 off→on 触发，on→on 不触发 | |
| T2.7 | V3 | `subscribe_trigger` on→off 过滤 | 只有 on→off 触发 | |
| T2.8 | V3 | `subscribe_trigger` numeric_state above 80 | 亮度>80 触发，<=80 不触发 | |
| T2.9 | V3 | `unsubscribe_events` 取消订阅 | 取消后不再收到事件 | |
| T2.10 | V3 | WebSocket 断线重连 | 5 秒内自动重连并重新订阅 | |
| T2.11 | V3 | 长时间空闲（5 分钟） | 连接保持，ping/pong 保活正常 | |

### 4.3 Phase 3: 真实设备接入

| 编号 | 验证项 | 操作 | 预期结果 | 实际结果 |
|------|--------|------|----------|----------|
| T3.1 | V5 | Docker exec 安装 HACS | custom_components/hacs 目录存在 | |
| T3.2 | V5 | HA 重启后 HACS 出现在集成列表 | 可搜索到 HACS | |
| T3.3 | V5 | HACS GitHub 授权 | 需要浏览器操作，记录步骤 | |
| T3.4 | V5 | HACS 安装 Xiaomi Miot Auto | 集成安装成功 | |
| T3.5 | V5 | 发起 Xiaomi Miot 配置流 | 返回 schema，包含账号密码字段 | |
| T3.6 | V5 | 提交米家账号密码 | 配置流推进到下一步或 create_entry | |
| T3.7 | V5 | 查询自动发现的小米灯实体 | 实体 ID 以 light. 开头 | |
| T3.8 | V5 | API 控制小米灯开 | 灯亮 | |
| T3.9 | V5 | API 控制小米灯关 | 灯灭 | |
| T3.10 | V5 | API 调节小米灯亮度 | 亮度变化 | |
| T3.11 | V5 | WebSocket 订阅小米灯 state_changed | 灯状态变更时收到事件 | |
| T3.12 | V2 | 评估 Agent 引导式配置可行性 | 记录哪些步骤可程序化、哪些需人工 | |

### 4.4 Phase 4: 经验总结

| 编号 | 验证项 | 操作 | 预期结果 | 实际结果 |
|------|--------|------|----------|----------|
| T4.1 | V4 | 归纳 REST API 行为模式 | 每个端点有固定输入输出格式 | |
| T4.2 | V4 | 归纳 WebSocket API 行为模式 | 消息格式统一，id 匹配机制清晰 | |
| T4.3 | V4 | 归纳 Config Flow 模式 | 4 种模式可分类（账号/发现/OAuth/配对） | |
| T4.4 | V4 | 评估 Skill 可行性 | 至少 3 个操作流程可写成 Skill | |
| T4.5 | V4 | 编写 HA API 经验手册 | 覆盖所有测试过的端点和行为 | |

---

## 5. 经验沉淀评估标准

### 5.1 可写成 Skill 的判定条件

一个操作流程可以写成 Skill，当且仅当同时满足以下条件：

1. **步骤固定** — 每次执行的步骤序列相同，不因环境变化而改变顺序
2. **可程序化** — 每个步骤都可以通过 API 调用或命令行完成，不需要 GUI 操作
3. **输入可枚举** — 所需参数的类型和取值范围可以预先定义（如 entity_id 是字符串、brightness 是 0-100 的整数）
4. **错误可诊断** — 常见错误有明确的排查路径（如 401 → Token 过期、404 → 实体不存在）
5. **结果可验证** — 操作完成后可以通过 API 查询验证结果（如开灯后 GET state 确认为 on）

### 5.2 不能写成 Skill 的判定条件

一个操作流程不能写成 Skill，如果满足以下任一条件：

1. **需要现场判断** — 复杂网络问题（IP 冲突、防火墙规则、子网隔离），排查路径不固定
2. **需要物理操作** — 按配对按钮、扫码、插拔设备
3. **依赖特定硬件型号** — 不同型号的配置参数差异大，无法用统一 schema 描述
4. **需要浏览器交互** — OAuth 认证、验证码输入、Web 表单提交
5. **结果不可预测** — 操作结果依赖外部系统状态（如云端 API 限流、设备离线）

### 5.3 边界情况处理

| 情况 | 判定 | 处理方式 |
|------|------|----------|
| 设备离线时调用服务 | 可 Skill 化 | API 返回错误，Skill 据此提示用户 |
| 集成配置流中途失败 | 可 Skill 化 | 捕获错误，回滚配置流，提示用户 |
| 多个同类型设备选择 | 可 Skill 化 | 列出设备让用户选择，Skill 执行后续步骤 |
| OAuth 认证 | 不可 Skill 化 | Skill 执行到 OAuth 步骤时暂停，引导用户在浏览器完成 |
| 蓝牙配对 | 不可 Skill 化 | Skill 输出操作指引，用户手动完成后 Skill 继续验证 |
| 固件升级 | 边界 | 升级本身可触发，但升级结果不可控，需人工确认 |

---

## 6. 后续对接路径

验证通过后，按以下路径将 HA 能力集成到 niu-agent 系统中：

### 6.1 封装为独立 MCP Server（ha-server）

```
mcp-servers/ha-server/
├── src/
│   └── niu_ha_server/
│       ├── __init__.py          # TOOL_SCHEMAS + 工具函数
│       ├── __main__.py          # 入口点
│       ├── connection.py        # WebSocket 长连接管理
│       ├── rest_client.py       # REST API 封装
│       ├── event_watcher.py     # 事件守护线程
│       └── config_flow.py       # 配置流辅助
└── pyproject.toml
```

**工具清单**：

| 工具名 | 功能 | 对应 API |
|--------|------|----------|
| `ha_get_states` | 查询实体状态 | GET /api/states |
| `ha_get_state` | 查询单个实体 | GET /api/states/{entity_id} |
| `ha_call_service` | 调用服务 | POST /api/services/{domain}/{service} |
| `ha_get_history` | 查询历史 | GET /api/history/period |
| `ha_list_devices` | 列出设备 | WS config/device_registry/list |
| `ha_list_areas` | 列出区域 | WS config/area_registry/list |
| `ha_init_config_flow` | 发起配置流 | WS config/integration/initialize |
| `ha_step_config_flow` | 推进配置流 | WS config/integration/step |
| `ha_delete_integration` | 删除集成 | WS config/integration/delete |
| `ha_subscribe_events` | 订阅事件 | WS subscribe_events |
| `ha_subscribe_trigger` | 订阅触发器 | WS subscribe_trigger |

### 6.2 同进程注册到 ToolRegistry

```python
# agent/mcp_loader.py 中添加
from niu_ha_server import register_tools

# 启动时注册
register_tools(get_registry())
```

### 6.3 虚拟磁盘映射工具

利用本系统已有的虚拟磁盘工具映射机制，将 HA 实体映射为虚拟文件系统：

```
/ha/
├── devices/                    # 设备列表（只读）
│   ├── xiaomi_light_living.txt  # 内容: {"state": "on", "brightness": 80, ...}
│   └── midea_ac_bedroom.txt
├── control/                    # 控制接口（写入触发操作）
│   ├── xiaomi_light_living/
│   │   ├── state              # echo "on" > state → 开灯
│   │   ├── brightness         # echo "50" > brightness → 调亮度
│   │   └── color              # echo "255,128,0" > color → 调颜色
│   └── midea_ac_bedroom/
│       ├── state              # echo "on" > state → 开空调
│       └── temperature        # echo "26" > temperature → 调温度
├── events/                     # 事件流（只读，tail -f 实时输出）
│   └── state_changed.log
└── config/                     # 配置操作
    ├── integrations/           # 集成管理
    └── flows/                  # 配置流
```

### 6.4 定时任务推送事件到 Agent 工作记忆

```python
# 利用 scheduler-server 的定时任务能力
# 事件守护线程收到 state_changed 后，通过 ToolRegistry 调用 memory-server

from agent.tool_registry import get_registry

def on_ha_event(event_data):
    """HA 事件 → Agent 工作记忆"""
    registry = get_registry()

    entity_id = event_data["data"]["entity_id"]
    new_state = event_data["data"]["new_state"]["state"]

    # 写入工作记忆
    registry.get("memory-server/user_memory_remember")(
        content=f"智能家居事件: {entity_id} 状态变为 {new_state}",
        type="ha_event"
    )

    # 如果是重要事件（如空调异常关闭），直接推送
    if entity_id.startswith("climate.") and new_state == "unavailable":
        registry.get("brain-region-server/brain_region_activate")(
            region="alert",
            reason=f"空调设备离线: {entity_id}"
        )
```

### 6.5 Skill 体系固化接入经验

```
config/skills/
├── ha-setup.md              # HA 环境搭建 Skill
├── ha-device-control.md     # 设备控制 Skill
├── ha-config-flow.md        # 配置流引导 Skill
├── ha-event-subscribe.md    # 事件订阅 Skill
└── ha-troubleshooting.md    # 常见问题排查 Skill
```

每个 Skill 文件包含：
- 触发条件（何时使用）
- 输入参数（需要用户提供什么）
- 执行步骤（调用哪些 MCP 工具，顺序如何）
- 验证方法（如何确认操作成功）
- 回滚方案（失败时如何恢复）
- 人工介入点（哪些步骤需要用户操作）

### 6.6 实施时间线

| 阶段 | 内容 | 预计时间 | 前置条件 |
|------|------|----------|----------|
| 验证期 | Phase 1-4 测试 | 1-2 天 | Docker + HA 环境 |
| 开发期 | ha-server MCP Server 开发 | 2-3 天 | 验证通过 |
| 集成期 | ToolRegistry 注册 + 虚拟磁盘映射 | 1 天 | ha-server 完成 |
| 事件期 | 事件守护线程 + 工作记忆推送 | 1 天 | 集成完成 |
| Skill 期 | 经验固化 + Skill 编写 | 1-2 天 | 事件期完成 |
| 星闪期 | NearLink 开源后评估 | 7月15日后 | 星闪 SDK 发布 |

---

## 附录 A: HA REST API 速查

| 方法 | 端点 | 用途 |
|------|------|------|
| GET | `/api/` | 健康检查 |
| GET | `/api/config` | 获取 HA 配置 |
| GET | `/api/states` | 获取所有实体状态 |
| GET | `/api/states/{entity_id}` | 获取单个实体状态 |
| POST | `/api/states/{entity_id}` | 设置实体状态（不推荐，用 call_service） |
| GET | `/api/events` | 列出所有事件类型 |
| GET | `/api/services` | 列出所有服务 |
| POST | `/api/services/{domain}/{service}` | 调用服务 |
| GET | `/api/history/period` | 查询历史 |
| GET | `/api/history/period/{timestamp}` | 查询指定时间后的历史 |
| GET | `/api/logbook` | 查询日志 |
| GET | `/api/logbook/{timestamp}` | 查询指定时间后的日志 |
| POST | `/api/template` | 渲染 Jinja2 模板 |
| GET | `/api/error_log` | 获取错误日志 |
| POST | `/api/config/homeassistant/restart` | 重启 HA |
| POST | `/api/config/homeassistant/check_config` | 检查配置 |

## 附录 B: HA WebSocket API 消息类型

| 类型 | 用途 | 方向 |
|------|------|------|
| `auth` | 认证 | 客户端→服务端 |
| `auth_required` | 请求认证 | 服务端→客户端 |
| `auth_ok` | 认证成功 | 服务端→客户端 |
| `auth_invalid` | 认证失败 | 服务端→客户端 |
| `get_states` | 获取所有实体状态 | 客户端→服务端 |
| `get_services` | 获取所有服务 | 客户端→服务端 |
| `get_config` | 获取配置 | 客户端→服务端 |
| `call_service` | 调用服务 | 客户端→服务端 |
| `subscribe_events` | 订阅事件 | 客户端→服务端 |
| `unsubscribe_events` | 取消订阅 | 客户端→服务端 |
| `subscribe_trigger` | 订阅触发器 | 客户端→服务端 |
| `ping` | 心跳 | 客户端→服务端 |
| `pong` | 心跳回复 | 服务端→客户端 |
| `result` | 命令结果 | 服务端→客户端 |
| `event` | 事件推送 | 服务端→客户端 |
| `config/integration/initialize` | 发起配置流 | 客户端→服务端 |
| `config/integration/step` | 推进配置流 | 客户端→服务端 |
| `config/integration/delete` | 删除集成 | 客户端→服务端 |
| `config/integration/list` | 列出集成 | 客户端→服务端 |
| `config/device_registry/list` | 列出设备 | 客户端→服务端 |
| `config/area_registry/list` | 列出区域 | 客户端→服务端 |
| `config/entity_registry/list` | 列出实体注册信息 | 客户端→服务端 |

## 附录 C: 测试脚本依赖

```bash
pip install requests websockets
```

所有测试脚本基于 Python 3.11+，仅依赖 `requests` 和 `websockets` 两个库。
