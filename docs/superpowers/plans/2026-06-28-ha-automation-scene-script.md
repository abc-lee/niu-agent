# HA 自动化/场景/脚本管理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 ha-server MCP 中新增 ha_automation / ha_scene / ha_script 三个工具，提供自动化、场景、脚本的完整 CRUD + 操作能力。

**Architecture:** 三个工具统一用 `name` 作为用户标识，内部通过 `/api/states` + entity_registry 映射到 HA 的 config_key。REST API 做 CRUD，WebSocket 做批量查询，HA 服务调用做 enable/disable/trigger/activate/run/snapshot。

**Tech Stack:** Python 3.11+, HA REST API + WebSocket API, pytest (真实 HA 环境)

---

## 修改的文件

| 文件 | 改动 |
|------|------|
| `mcp-servers/ha-server/src/niu_ha_server/__init__.py` | 新增 3 个 TOOL_SCHEMAS + 辅助函数 + 3 个工具实现函数 |
| `config/disk/ha-server.yaml` | 新增 3 个工具条目 + 更新 description |
| `tests/test_ha_automation.py` | 新建，自动化/场景/脚本的集成测试 |

## 共用辅助函数

三个工具共用以下辅助函数（在 Task 1 中实现）：

```python
def _resolve_config_key(ha_url, headers, domain, entity_id, entity_registry=None):
    """通过 entity_id 查找 config_key（entity_registry 的 unique_id）。
    脚本域直接从 entity_id 提取 slug，无需查注册表。
    entity_registry: 可选的预查询结果，避免重复 WebSocket 调用。"""

def _find_entity_by_name(states, domain, name):
    """在 states 列表中按 friendly_name 匹配 entity_id。
    精确匹配优先，无精确匹配时模糊匹配。"""

def _fetch_domain_states(ha_url, headers, domain):
    """GET /api/states 过滤指定 domain，返回 [{name, entity_id, state, ...}]"""

def _fetch_entity_registry(ha_url, headers):
    """通过 WebSocket 一次性获取 entity_registry，返回 [{entity_id, unique_id, ...}]。
    避免在循环中重复建立 WebSocket 连接。"""

def _verify_entity_exists(ha_url, headers, domain, config_key, timeout=3):
    """创建后验证 entity 已注册。返回 entity_id 或 None。"""
```

## 关键设计决策（审查后修正）

1. **脚本 POST 请求体直接传 config** — HA 的 `EditKeyBasedConfigView._write_value` 会自动将请求体挂到 `data[config_key]` 下，所以 `POST /api/config/script/config/{slug}` 的请求体应为 `config` 本身，而非 `{slug: config}` 包装
2. **snapshot 直接读取各 entity 当前状态** — `scene.create` 创建的是临时场景（内存中，不可通过配置 API 持久化），所以改为读取各 entity 当前 state + attributes，手动构建 scene config 后持久化
3. **create/update 前移除用户传入的 id** — HA 内部会自动设置 `id = config_key`，用户传入的 `id` 会覆盖导致不一致，所以创建前 `config.pop("id", None)`
4. **_resolve_config_key 直接使用传入参数** — 不再重复调用 `_read_config()` + `_get_ha_client()`，支持传入预查询的 `entity_registry` 避免循环中重复 WebSocket 连接
5. **create 后验证 entity 已注册** — HA 创建后需短暂时间注册 entity，create 操作后通过 states API 验证
6. **测试使用真实 entity** — 用 fixture 从 ha_status 动态获取真实存在的 entity_id，避免使用不存在的 `light.test`

---

### Task 1: 辅助函数 + ha_automation TOOL_SCHEMA

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`

- [ ] **Step 1: 添加辅助函数**

在 `_get_ha_client()` 函数之后（约 L180）添加：

```python
def _find_entity_by_name(states, domain, name):
    """在 states 列表中按 friendly_name 匹配 entity_id"""
    prefix = f"{domain}."
    candidates = [s for s in states if s.get("entity_id", "").startswith(prefix)]
    # 精确匹配 friendly_name 或 entity_id
    for s in candidates:
        if s.get("attributes", {}).get("friendly_name", "") == name:
            return s["entity_id"]
        if s["entity_id"] == f"{domain}.{name}":
            return s["entity_id"]
    # 模糊匹配
    name_lower = name.lower()
    for s in candidates:
        fn = s.get("attributes", {}).get("friendly_name", "").lower()
        if name_lower in fn or fn in name_lower:
            return s["entity_id"]
    return None


def _fetch_entity_registry(ha_url, headers):
    """通过 WebSocket 一次性获取 entity_registry"""
    token = headers.get("Authorization", "").replace("Bearer ", "")
    commands = [{"type": "config/entity_registry/list"}]
    results = _ws_batch_call(ha_url, token, commands)
    if not results or not results[0]:
        return []
    return results[0]


def _resolve_config_key(ha_url, headers, domain, entity_id, entity_registry=None):
    """通过 entity_id 查找 config_key（entity_registry 的 unique_id）。
    entity_registry: 可选的预查询结果，避免重复 WebSocket 调用。"""
    if domain == "script":
        return entity_id.split(".", 1)[1]
    if entity_registry is None:
        entity_registry = _fetch_entity_registry(ha_url, headers)
    for entry in entity_registry:
        if entry.get("entity_id") == entity_id:
            return entry.get("unique_id")
    return None


def _fetch_domain_states(ha_url, headers, domain):
    """GET /api/states 过滤指定 domain"""
    try:
        resp = _requests.get(f"{ha_url}/api/states", headers=headers, timeout=15)
        if resp.status_code != 200:
            return []
        prefix = f"{domain}."
        return [s for s in resp.json() if s.get("entity_id", "").startswith(prefix)]
    except Exception:
        return []


def _verify_entity_exists(ha_url, headers, domain, config_key, timeout=3):
    """创建后验证 entity 已注册。返回 entity_id 或 None。"""
    import time
    prefix = f"{domain}."
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = _requests.get(f"{ha_url}/api/states", headers=headers, timeout=10)
            if resp.status_code == 200:
                for s in resp.json():
                    eid = s.get("entity_id", "")
                    if eid.startswith(prefix) and eid == f"{domain}.{config_key}":
                        return eid
            time.sleep(0.3)
        except Exception:
            time.sleep(0.3)
    return None
```

- [ ] **Step 2: 添加 ha_automation TOOL_SCHEMA**

在 TOOL_SCHEMAS 字典中添加：

```python
    "ha_automation": {
        "name": "ha_automation",
        "description": "管理自动化：创建/查看/修改/删除/启用/禁用/手动触发自动化。自动化是条件触发持续生效的规则（如'湿度>70%开除湿'、'日落开灯'）。立即执行一次用 ha_control，定时一次用 scheduler。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "enable", "disable", "trigger"],
                    "description": "操作类型：list=列出所有，get=查看配置，create=创建，update=更新，delete=删除，enable=启用，disable=禁用，trigger=手动触发",
                },
                "name": {
                    "type": "string",
                    "description": "自动化名称（get/create/update/delete/enable/disable/trigger 时必填）",
                },
                "config": {
                    "type": "object",
                    "description": "自动化配置 JSON。triggers: 触发条件列表，conditions: 执行条件列表，actions: 动作列表。trigger platform: state/numeric_state/time/time_pattern/sun/zone/event/template/mqtt/calendar/webhook/homeassistant。condition type: state/numeric_state/time/sun/zone/template/trigger/and/or/not。action type: 服务调用(用action键)/delay/wait_for_trigger/wait_template/choose/if/repeat/parallel/condition/variables/event/scene/stop。mode: single|restart|queued|parallel",
                },
                "confirm": {
                    "type": "boolean",
                    "description": "删除确认（delete 操作第二次调用时传 true）",
                },
                "detail": {
                    "type": "boolean",
                    "description": "list 时是否返回完整配置（默认 false，只返回摘要）",
                },
            },
            "required": ["action"],
        },
    },
```

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('mcp-servers/ha-server/src/niu_ha_server/__init__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat: add helper functions and ha_automation TOOL_SCHEMA"
```

---

### Task 2: ha_automation 实现函数

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`
- Create: `tests/test_ha_automation.py`

- [ ] **Step 1: 写 list 测试**

```python
"""HA 自动化/场景/脚本集成测试 — 使用真实 HA 环境"""
import sys
sys.path.insert(0, "mcp-servers/ha-server/src")
from niu_ha_server import ha_automation, ha_setup, ha_scene, ha_script


def _ensure_connected():
    """确保 HA 已连接"""
    result = ha_setup()
    if not result.get("connected"):
        ha_url = os.environ.get("HA_URL", "http://homeassistant.local:8123")
        ha_token = os.environ.get("HA_TOKEN", "")
        if not ha_token:
            pytest.skip("HA_TOKEN not set")
        result = ha_setup(ha_url=ha_url, ha_token=ha_token)
        assert result.get("connected"), f"HA connection failed: {result}"


import os
import pytest


class TestHaAutomation:
    def test_list(self):
        """list 操作返回自动化摘要列表"""
        _ensure_connected()
        result = ha_automation(action="list")
        assert isinstance(result, dict)
        assert "automations" in result or "error" in result
        if "automations" in result:
            for a in result["automations"]:
                assert "name" in a
                assert "entity_id" in a

    def test_list_detail(self):
        """list detail=true 返回完整配置"""
        _ensure_connected()
        result = ha_automation(action="list", detail=True)
        assert isinstance(result, dict)
        if "automations" in result and result["automations"]:
            first = result["automations"][0]
            assert "config" in first
```

- [ ] **Step 2: 实现 ha_automation 函数（list + get）**

在 `__init__.py` 的工具函数区域（ha_integrate 之后）添加：

```python
def ha_automation(action: str, name: str = "", config: dict = None, confirm: bool = False, detail: bool = False, **kwargs) -> dict:
    """管理自动化"""
    cfg = _read_config()
    ha_url, headers, err = _get_ha_client(cfg)
    if err:
        return {"error": err}

    if action == "list":
        states = _fetch_domain_states(ha_url, headers, "automation")
        # detail=true 时一次性获取 entity_registry，避免循环中重复 WebSocket 连接
        entity_registry = None
        if detail:
            entity_registry = _fetch_entity_registry(ha_url, headers)
        automations = []
        for s in states:
            attrs = s.get("attributes", {})
            entry = {
                "name": attrs.get("friendly_name", s["entity_id"]),
                "entity_id": s["entity_id"],
                "state": s.get("state", "off"),
                "last_triggered": attrs.get("last_triggered"),
            }
            if detail:
                config_key = _resolve_config_key(ha_url, headers, "automation", s["entity_id"], entity_registry)
                if config_key:
                    try:
                        resp = _requests.get(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, timeout=10)
                        if resp.status_code == 200:
                            entry["config"] = resp.json()
                    except Exception:
                        pass
            automations.append(entry)
        return {"automations": automations}

    if action == "get":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化，请先 list 查看可用列表"}
        config_key = _resolve_config_key(ha_url, headers, "automation", entity_id)
        if not config_key:
            return {"error": f"无法解析自动化的配置 ID: {entity_id}"}
        try:
            resp = _requests.get(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code == 200:
                return {"name": name, "entity_id": entity_id, "config": resp.json()}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    # create / update / delete / enable / disable / trigger 见 Task 3
    return {"error": f"未知操作: {action}"}
```

- [ ] **Step 3: 运行 list 测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && HA_TOKEN=$(cat ~/.niu/ha-config.json | python3 -c "import sys,json; print(json.load(sys.stdin).get('ha_token',''))") python -m pytest tests/test_ha_automation.py::TestHaAutomation::test_list -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py tests/test_ha_automation.py
git commit -m "feat: implement ha_automation list + get"
```

---

### Task 3: ha_automation create/update/delete/enable/disable/trigger

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`
- Modify: `tests/test_ha_automation.py`

- [ ] **Step 1: 补全 ha_automation 函数**

在 Task 2 的 `# create / update / delete / enable / disable / trigger 见 Task 3` 处替换为：

```python
    import uuid

    if action == "create":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        config.pop("id", None)  # 移除用户可能传入的 id，由 HA 内部设置
        config_key = uuid.uuid4().hex
        config["id"] = config_key
        config["alias"] = name
        try:
            resp = _requests.post(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                # 验证 entity 已注册
                actual_entity_id = _verify_entity_exists(ha_url, headers, "automation", config_key)
                return {"success": True, "name": name, "entity_id": actual_entity_id or f"automation.{config_key}", "config_key": config_key}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "update":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        config_key = _resolve_config_key(ha_url, headers, "automation", entity_id)
        if not config_key:
            return {"error": f"无法解析自动化的配置 ID: {entity_id}"}
        config.pop("id", None)  # 移除用户可能传入的 id
        config["id"] = config_key
        config["alias"] = name
        try:
            resp = _requests.post(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "entity_id": entity_id}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "delete":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        if not confirm:
            attrs = next((s.get("attributes", {}) for s in states if s["entity_id"] == entity_id), {})
            return {"preview": True, "name": name, "entity_id": entity_id, "state": next((s.get("state") for s in states if s["entity_id"] == entity_id), ""), "last_triggered": attrs.get("last_triggered"), "message": "确认删除？请再次调用并传 confirm=true"}
        config_key = _resolve_config_key(ha_url, headers, "automation", entity_id)
        if not config_key:
            return {"error": f"无法解析自动化的配置 ID: {entity_id}"}
        try:
            resp = _requests.delete(f"{ha_url}/api/config/automation/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                return {"success": True, "deleted": name}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action in ("enable", "disable"):
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        service = "automation.turn_on" if action == "enable" else "automation.turn_off"
        try:
            resp = _requests.post(f"{ha_url}/api/services/{service.replace('.', '/', 1)}", headers=headers, json={"entity_id": entity_id}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "action": action + "d"}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "trigger":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "automation")
        entity_id = _find_entity_by_name(states, "automation", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的自动化"}
        try:
            resp = _requests.post(f"{ha_url}/api/services/automation/trigger", headers=headers, json={"entity_id": entity_id}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "triggered": True}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}
```

- [ ] **Step 2: 添加 CRUD 测试**

在 `tests/test_ha_automation.py` 的 `TestHaAutomation` 类中添加：

```python
    def test_create_and_get(self):
        """创建自动化后可以 get 到"""
        _ensure_connected()
        # 使用真实存在的 entity（从 ha_status 获取第一个灯设备）
        from niu_ha_server import ha_status
        status = ha_status()
        real_entity = None
        if status.get("connected") and status.get("devices"):
            light = next((d for d in status["devices"] if d["entity_id"].startswith("light.")), None)
            if light:
                real_entity = light["entity_id"]
        if not real_entity:
            pytest.skip("No light entity available for test")
        result = ha_automation(action="create", name="测试自动删除", config={
            "triggers": [{"platform": "time", "at": "08:00:00"}],
            "actions": [{"action": "light.turn_on", "target": {"entity_id": real_entity}}],
            "mode": "single",
        })
        assert result.get("success"), f"创建失败: {result}"
        import time; time.sleep(0.5)  # 等待 HA 注册 entity
        # 验证 get
        get_result = ha_automation(action="get", name="测试自动删除")
        assert get_result.get("config"), f"获取配置失败: {get_result}"
        # 清理
        ha_automation(action="delete", name="测试自动删除", confirm=True)

    def test_delete_preview(self):
        """delete 不带 confirm 返回预览"""
        _ensure_connected()
        # 先创建
        ha_automation(action="create", name="测试删除预览", config={
            "triggers": [{"platform": "time", "at": "09:00:00"}],
            "actions": [{"action": "persistent_notification.create", "data": {"message": "test"}}],
            "mode": "single",
        })
        import time; time.sleep(0.5)
        result = ha_automation(action="delete", name="测试删除预览")
        assert result.get("preview"), f"应返回预览: {result}"
        # 确认删除
        result = ha_automation(action="delete", name="测试删除预览", confirm=True)
        assert result.get("success"), f"删除失败: {result}"

    def test_enable_disable(self):
        """启用/禁用自动化"""
        _ensure_connected()
        # 先创建
        ha_automation(action="create", name="测试开关", config={
            "triggers": [{"platform": "time", "at": "10:00:00"}],
            "actions": [{"action": "persistent_notification.create", "data": {"message": "test"}}],
            "mode": "single",
        })
        import time; time.sleep(0.5)
        # 禁用
        result = ha_automation(action="disable", name="测试开关")
        assert result.get("success"), f"禁用失败: {result}"
        # 启用
        result = ha_automation(action="enable", name="测试开关")
        assert result.get("success"), f"启用失败: {result}"
        # 清理
        ha_automation(action="delete", name="测试开关", confirm=True)

    def test_name_not_found(self):
        """名称不存在时返回错误"""
        _ensure_connected()
        result = ha_automation(action="get", name="不存在的自动化_xyz")
        assert "error" in result
```

- [ ] **Step 3: 运行全部自动化测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_ha_automation.py -v`
Expected: 6 passed

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py tests/test_ha_automation.py
git commit -m "feat: implement ha_automation full CRUD + enable/disable/trigger"
```

---

### Task 4: ha_scene + ha_script TOOL_SCHEMA 和实现

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`
- Modify: `tests/test_ha_automation.py`

- [ ] **Step 1: 添加 TOOL_SCHEMAS**

在 TOOL_SCHEMAS 中添加：

```python
    "ha_scene": {
        "name": "ha_scene",
        "description": "管理场景：创建/查看/修改/删除/激活/快照场景。场景是多设备瞬间切换到预设状态（如'阅读模式'、'晚安模式'）。有序列有延时用 ha_script，条件触发用 ha_automation。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "activate", "snapshot"],
                    "description": "操作类型：list=列出所有，get=查看配置，create=创建，update=更新，delete=删除，activate=激活场景，snapshot=从当前设备状态创建场景快照",
                },
                "name": {"type": "string", "description": "场景名称"},
                "config": {
                    "type": "object",
                    "description": "场景配置 JSON。entities: 设备状态快照，键为 entity_id，值为目标状态字典。支持 light(亮度/色温)、climate(温度/模式)、switch、lock、cover(位置)、fan(转速)、humidifier(湿度)",
                },
                "confirm": {"type": "boolean", "description": "删除确认"},
                "detail": {"type": "boolean", "description": "list 时是否返回完整配置"},
                "entity_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "snapshot 操作要快照的设备 entity_id 列表",
                },
            },
            "required": ["action"],
        },
    },
    "ha_script": {
        "name": "ha_script",
        "description": "管理脚本：创建/查看/修改/删除/运行脚本。脚本是有序列、有延时的多步骤操作（如'先关灯等5秒再锁门'）。瞬间切换用 ha_scene，条件触发用 ha_automation。sequence 动作类型与自动化 actions 相同。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "delete", "run"],
                    "description": "操作类型：list=列出所有，get=查看配置，create=创建，update=更新，delete=删除，run=执行脚本",
                },
                "name": {"type": "string", "description": "脚本名称"},
                "config": {
                    "type": "object",
                    "description": "脚本配置 JSON。alias: 名称，mode: single|restart|queued|parallel，sequence: 步骤列表。步骤类型: 服务调用(action键)/delay/wait_for_trigger/choose/if/repeat/parallel/condition",
                },
                "confirm": {"type": "boolean", "description": "删除确认"},
                "detail": {"type": "boolean", "description": "list 时是否返回完整配置"},
            },
            "required": ["action"],
        },
    },
```

- [ ] **Step 2: 实现 ha_scene 函数**

在 ha_automation 函数之后添加：

```python
def ha_scene(action: str, name: str = "", config: dict = None, confirm: bool = False, detail: bool = False, entity_ids: list = None, **kwargs) -> dict:
    """管理场景"""
    cfg = _read_config()
    ha_url, headers, err = _get_ha_client(cfg)
    if err:
        return {"error": err}
    import uuid

    if action == "list":
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_registry = None
        if detail:
            entity_registry = _fetch_entity_registry(ha_url, headers)
        scenes = []
        for s in states:
            attrs = s.get("attributes", {})
            entry = {"name": attrs.get("friendly_name", s["entity_id"]), "entity_id": s["entity_id"], "state": s.get("state", "off")}
            if detail:
                config_key = _resolve_config_key(ha_url, headers, "scene", s["entity_id"], entity_registry)
                if config_key:
                    try:
                        resp = _requests.get(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, timeout=10)
                        if resp.status_code == 200:
                            entry["config"] = resp.json()
                    except Exception:
                        pass
            scenes.append(entry)
        return {"scenes": scenes}

    if action == "get":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的场景"}
        config_key = _resolve_config_key(ha_url, headers, "scene", entity_id)
        if not config_key:
            return {"error": f"无法解析场景的配置 ID: {entity_id}"}
        try:
            resp = _requests.get(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code == 200:
                return {"name": name, "entity_id": entity_id, "config": resp.json()}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "create":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        config.pop("id", None)  # 移除用户可能传入的 id
        config_key = uuid.uuid4().hex
        config["name"] = name
        config["id"] = config_key
        try:
            resp = _requests.post(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                actual_entity_id = _verify_entity_exists(ha_url, headers, "scene", config_key)
                return {"success": True, "name": name, "entity_id": actual_entity_id or f"scene.{config_key}"}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "update":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的场景"}
        config_key = _resolve_config_key(ha_url, headers, "scene", entity_id)
        if not config_key:
            return {"error": f"无法解析场景的配置 ID: {entity_id}"}
        config.pop("id", None)
        config["name"] = name
        config["id"] = config_key
        try:
            resp = _requests.post(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "entity_id": entity_id}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "delete":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的场景"}
        if not confirm:
            return {"preview": True, "name": name, "entity_id": entity_id, "message": "确认删除？请再次调用并传 confirm=true"}
        config_key = _resolve_config_key(ha_url, headers, "scene", entity_id)
        if not config_key:
            return {"error": f"无法解析场景的配置 ID: {entity_id}"}
        try:
            resp = _requests.delete(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                return {"success": True, "deleted": name}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "activate":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "scene")
        entity_id = _find_entity_by_name(states, "scene", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的场景"}
        try:
            resp = _requests.post(f"{ha_url}/api/services/scene/turn_on", headers=headers, json={"entity_id": entity_id}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "activated": True}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "snapshot":
        """从当前设备状态创建场景快照并持久化。
        scene.create 创建的是临时场景（内存中），无法通过配置 API 持久化。
        所以改为直接读取各 entity 当前状态，手动构建 scene config 后持久化。"""
        if not name or not entity_ids:
            return {"error": "name 和 entity_ids 参数必填"}
        entities_config = {}
        for eid in entity_ids:
            try:
                resp = _requests.get(f"{ha_url}/api/states/{eid}", headers=headers, timeout=10)
                if resp.status_code == 200:
                    state_data = resp.json()
                    entities_config[eid] = {"state": state_data["state"]}
                    # 提取关键属性（亮度、色温等）
                    attrs = state_data.get("attributes", {})
                    for key in ("brightness", "color_temp_kelvin", "target_temperature", "hvac_mode", "percentage", "current_cover_position", "target_humidity", "preset_mode", "fan_mode"):
                        if key in attrs:
                            entities_config[eid][key] = attrs[key]
            except Exception:
                pass
        if not entities_config:
            return {"error": "无法读取任何设备状态，请检查 entity_ids"}
        config_key = uuid.uuid4().hex
        scene_config = {"name": name, "id": config_key, "entities": entities_config}
        try:
            resp = _requests.post(f"{ha_url}/api/config/scene/config/{config_key}", headers=headers, json=scene_config, timeout=10)
            if resp.status_code in (200, 201):
                actual_entity_id = _verify_entity_exists(ha_url, headers, "scene", config_key)
                return {"success": True, "name": name, "entity_id": actual_entity_id or f"scene.{config_key}", "entities": list(entities_config.keys())}
            return {"error": f"快照持久化失败: HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"未知操作: {action}"}
```

- [ ] **Step 3: 实现 ha_script 函数**

在 ha_scene 之后添加：

```python
def ha_script(action: str, name: str = "", config: dict = None, confirm: bool = False, detail: bool = False, **kwargs) -> dict:
    """管理脚本"""
    cfg = _read_config()
    ha_url, headers, err = _get_ha_client(cfg)
    if err:
        return {"error": err}
    import re
    import uuid

    if action == "list":
        states = _fetch_domain_states(ha_url, headers, "script")
        scripts = []
        for s in states:
            attrs = s.get("attributes", {})
            entry = {"name": attrs.get("friendly_name", s["entity_id"]), "entity_id": s["entity_id"], "state": s.get("state", "off")}
            if detail:
                slug = s["entity_id"].split(".", 1)[1]
                try:
                    resp = _requests.get(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
                    if resp.status_code == 200:
                        entry["config"] = resp.json()
                except Exception:
                    pass
            scripts.append(entry)
        return {"scripts": scripts}

    if action == "get":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "script")
        entity_id = _find_entity_by_name(states, "script", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        slug = entity_id.split(".", 1)[1]
        try:
            resp = _requests.get(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
            if resp.status_code == 200:
                return {"name": name, "entity_id": entity_id, "config": resp.json()}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "create":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        slug = re.sub(r'[^a-z0-9_]', '_', name.lower()).strip('_')
        if not slug:  # 非 ASCII 名称（如中文）会产生空字符串，回退到 UUID hex
            slug = uuid.uuid4().hex
        config.pop("id", None)
        config["alias"] = name
        # 关键：POST 请求体直接传 config，不是 {slug: config}
        # HA 的 EditKeyBasedConfigView._write_value 会自动将请求体挂到 data[config_key] 下
        try:
            resp = _requests.post(f"{ha_url}/api/config/script/config/{slug}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "entity_id": f"script.{slug}"}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "update":
        if not name or not config:
            return {"error": "name 和 config 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "script")
        entity_id = _find_entity_by_name(states, "script", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        slug = entity_id.split(".", 1)[1]
        config.pop("id", None)
        config["alias"] = name
        # 同 create：直接传 config
        try:
            resp = _requests.post(f"{ha_url}/api/config/script/config/{slug}", headers=headers, json=config, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "entity_id": entity_id}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "delete":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "script")
        entity_id = _find_entity_by_name(states, "script", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        if not confirm:
            return {"preview": True, "name": name, "entity_id": entity_id, "message": "确认删除？请再次调用并传 confirm=true"}
        slug = entity_id.split(".", 1)[1]
        try:
            resp = _requests.delete(f"{ha_url}/api/config/script/config/{slug}", headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                return {"success": True, "deleted": name}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    if action == "run":
        if not name:
            return {"error": "name 参数必填"}
        states = _fetch_domain_states(ha_url, headers, "script")
        entity_id = _find_entity_by_name(states, "script", name)
        if not entity_id:
            return {"error": f"未找到名为 '{name}' 的脚本"}
        try:
            resp = _requests.post(f"{ha_url}/api/services/script/turn_on", headers=headers, json={"entity_id": entity_id}, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "name": name, "running": True}
            return {"error": f"HA API 返回 {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"error": str(e)}

    return {"error": f"未知操作: {action}"}
```

- [ ] **Step 4: 添加场景和脚本测试**

在 `tests/test_ha_automation.py` 中添加：

```python
class TestHaScene:
    def test_list(self):
        _ensure_connected()
        result = ha_scene(action="list")
        assert isinstance(result, dict)
        assert "scenes" in result or "error" in result

    def test_create_activate_delete(self):
        """创建场景并激活，使用真实 entity"""
        _ensure_connected()
        # 从 ha_status 获取真实 entity
        from niu_ha_server import ha_status
        status = ha_status()
        real_entity = None
        if status.get("connected") and status.get("devices"):
            light = next((d for d in status["devices"] if d["entity_id"].startswith("light.")), None)
            if light:
                real_entity = light["entity_id"]
        if not real_entity:
            pytest.skip("No light entity available for test")
        # 创建场景
        result = ha_scene(action="create", name="测试场景删除", config={
            "entities": {real_entity: {"state": "on", "brightness": 128}}
        })
        assert result.get("success"), f"创建失败: {result}"
        # 激活（只测试 API 调用成功，不验证设备状态变化）
        result = ha_scene(action="activate", name="测试场景删除")
        assert result.get("success"), f"激活失败: {result}"
        # 删除
        result = ha_scene(action="delete", name="测试场景删除", confirm=True)
        assert result.get("success"), f"删除失败: {result}"

    def test_snapshot_with_real_entities(self):
        """snapshot 使用真实 entity_ids 创建快照"""
        _ensure_connected()
        from niu_ha_server import ha_status
        status = ha_status()
        if not status.get("connected") or not status.get("devices"):
            pytest.skip("No devices available for snapshot test")
        # 取前 2 个真实 entity
        entity_ids = [d["entity_id"] for d in status["devices"][:2]]
        result = ha_scene(action="snapshot", name="测试快照删除", entity_ids=entity_ids)
        assert result.get("success"), f"快照失败: {result}"
        # 删除
        ha_scene(action="delete", name="测试快照删除", confirm=True)


class TestHaScript:
    def test_list(self):
        _ensure_connected()
        result = ha_script(action="list")
        assert isinstance(result, dict)
        assert "scripts" in result or "error" in result

    def test_create_run_delete(self):
        """创建脚本并运行，使用 delay 避免依赖真实 entity"""
        _ensure_connected()
        result = ha_script(action="create", name="测试脚本删除", config={
            "mode": "single",
            "sequence": [{"delay": {"seconds": 1}}],
        })
        assert result.get("success"), f"创建失败: {result}"
        import time; time.sleep(0.5)
        # 运行
        result = ha_script(action="run", name="测试脚本删除")
        assert result.get("success"), f"运行失败: {result}"
        # 删除
        result = ha_script(action="delete", name="测试脚本删除", confirm=True)
        assert result.get("success"), f"删除失败: {result}"
```

- [ ] **Step 5: 运行全部测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_ha_automation.py -v`
Expected: 11 passed

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py tests/test_ha_automation.py
git commit -m "feat: implement ha_scene and ha_script with full CRUD + activate/snapshot/run"
```

---

### Task 5: 注册工具到 MCP Server + 磁盘映射

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`
- Modify: `config/disk/ha-server.yaml`

- [ ] **Step 1: 注册工具到 MCP Server**

在 `run_server()` 函数中，找到现有工具注册位置（搜索 `@server.call_tool` 或 `server.call_tool`），按现有模式注册三个新工具。具体代码取决于现有注册方式——找到 `ha_status`/`ha_control` 等工具的注册代码，复制模式添加 `ha_automation`/`ha_scene`/`ha_script`。

搜索 `@server.call_tool` 或 `"ha_status"` 在 call_tool handler 中的分发逻辑，添加：

```python
elif tool_name == "ha_automation":
    result = ha_automation(**arguments)
elif tool_name == "ha_scene":
    result = ha_scene(**arguments)
elif tool_name == "ha_script":
    result = ha_script(**arguments)
```

- [ ] **Step 2: 更新磁盘映射**

读取 `config/disk/ha-server.yaml`，更新 `description` 并新增 3 个工具条目。

description 改为：
```yaml
description: "智能家居 — 立即执行用 ha_control；定时一次用 scheduler；条件触发持续生效用 ha_automation；多设备瞬间切换用 ha_scene；有序列有延时用 ha_script"
```

在 `tools` 列表末尾添加 3 个工具条目（格式与现有工具一致）：

```yaml
  - name: ha_automation
    category: write
    short: "管理自动化"
    long: "管理自动化：创建/查看/修改/删除/启用/禁用/手动触发。自动化是条件触发持续生效的规则。立即执行一次用 ha_control，定时一次用 scheduler。trigger platform: state/numeric_state/time/time_pattern/sun/zone/event/template/mqtt/calendar。condition type: state/numeric_state/time/sun/zone/template/and/or/not。action type: 服务调用(用action键)/delay/wait_for_trigger/choose/if/repeat/parallel/scene/stop。mode: single|restart|queued|parallel"
    parameters:
      - name: action
        position: 1
        type: string
        required: true
        enum: [list, get, create, update, delete, enable, disable, trigger]
        description: "操作类型"
      - name: name
        type: string
        description: "自动化名称"
      - name: config
        type: object
        cli_format: json
        description: "自动化配置 JSON"
      - name: confirm
        type: string
        flag: confirm
        description: "删除确认，传 true"
      - name: detail
        type: string
        flag: detail
        description: "list 时返回完整配置"

  - name: ha_scene
    category: write
    short: "管理场景"
    long: "管理场景：创建/查看/修改/删除/激活/快照。场景是多设备瞬间切换到预设状态。有序列有延时用 ha_script，条件触发用 ha_automation。entities 支持设备: light(亮度/色温)/climate(温度/模式)/switch/lock/cover(位置)/fan(转速)/humidifier(湿度)"
    parameters:
      - name: action
        position: 1
        type: string
        required: true
        enum: [list, get, create, update, delete, activate, snapshot]
        description: "操作类型"
      - name: name
        type: string
        description: "场景名称"
      - name: config
        type: object
        cli_format: json
        description: "场景配置 JSON"
      - name: confirm
        type: string
        flag: confirm
        description: "删除确认，传 true"
      - name: detail
        type: string
        flag: detail
        description: "list 时返回完整配置"
      - name: entity_ids
        type: string
        flag: entity-ids
        description: "snapshot 操作要快照的设备 ID 列表"

  - name: ha_script
    category: write
    short: "管理脚本"
    long: "管理脚本：创建/查看/修改/删除/运行。脚本是有序列、有延时的多步骤操作。瞬间切换用 ha_scene，条件触发用 ha_automation。sequence 动作: 服务调用(action键)/delay/wait_for_trigger/choose/if/repeat/parallel/condition。mode: single|restart|queued|parallel"
    parameters:
      - name: action
        position: 1
        type: string
        required: true
        enum: [list, get, create, update, delete, run]
        description: "操作类型"
      - name: name
        type: string
        description: "脚本名称"
      - name: config
        type: object
        cli_format: json
        description: "脚本配置 JSON"
      - name: confirm
        type: string
        flag: confirm
        description: "删除确认，传 true"
      - name: detail
        type: string
        flag: detail
        description: "list 时返回完整配置"
```

- [ ] **Step 3: 验证语法和导入**

Run: `python -c "import ast; ast.parse(open('mcp-servers/ha-server/src/niu_ha_server/__init__.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: 端到端验证**

启动程序（`./niu`），在对话中测试：
1. "列出我的所有自动化" → 应调用 ha_automation list
2. "创建一个自动化：每天早上8点开书房灯" → 应调用 ha_automation create
3. "查看阅读模式场景" → 应调用 ha_scene get
4. "创建一个脚本：先关灯等5秒再锁门" → 应调用 ha_script create

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py config/disk/ha-server.yaml
git commit -m "feat: register ha_automation/ha_scene/ha_script to MCP server + disk mapping"
```

---

## 每步验证

每个 Task 完成后：
1. `python -c "import ast; ast.parse(open('mcp-servers/ha-server/src/niu_ha_server/__init__.py').read()); print('OK')"` — 语法检查
2. `python -c "from niu_ha_server import ha_automation, ha_scene, ha_script"` — 导入检查

## 功能验证

1. `ha_automation(action="list")` → 返回现有自动化列表
2. `ha_automation(action="create", name="测试", config={...})` → 创建成功
3. `ha_automation(action="get", name="测试")` → 能读取完整配置
4. `ha_automation(action="disable", name="测试")` → 禁用成功
5. `ha_automation(action="delete", name="测试", confirm=True)` → 删除成功
6. `ha_scene(action="list")` → 返回现有场景列表
7. `ha_script(action="list")` → 返回现有脚本列表
8. 在 Agent 对话中用自然语言触发这些工具
