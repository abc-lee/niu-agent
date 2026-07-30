# ha_get_image 工具实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 ha-server 新增 `ha_get_image` MCP 工具，下载 HA image 域图片实体（扫地机地图等）到本地，返回路径供 Agent 用 Markdown 展示。

**Architecture:** 通过 HA REST API `GET /api/image_proxy/{entity_id}` 下载 image 域图片实体的二进制数据，保存到 `~/.niu/tmp/` 目录（复用项目 `agent.tmp_dir.save_to_tmp()` 函数，带 fallback），返回本地路径。Agent 在聊天中用 `![](path)` Markdown 语法展示，chat.html 已有 `file://` 图片渲染逻辑（L1094-1118）。

**Tech Stack:** Python 3.11, requests, ha-server MCP 同进程架构

---

## 已验证的关键事实

1. **HA `image_proxy` API 可用**：`GET http://localhost:8123/api/image_proxy/image.18603118098_map` 返回 200, content-type `image/svg+xml`, 43293 bytes
2. **`image_proxy` 仅对 image 域有效，camera 域需 `camera_proxy` 端点**：实测 `GET /api/camera_proxy/image.18603118098_map` 返回 404，两个端点不同。本工具仅支持 image 域实体（如 `image.xxx_map`）。camera 域截图如需支持，需单独实现 `camera_proxy` 分支，留作后续需求。
3. **SVG 可被 `<img>` 渲染**：独立 SVG 文件（含 xmlns），无外部图片引用。Electron `<img src="file:///path.svg">` 预期可渲染（与 photo-server 的 PNG facebox 同路径机制），Task 5 运行验证中确认。
4. **chat.html 图片渲染已就绪**：L1094-1118 自动将 `/path` 转 `file://` URL，添加 `.chat-image` class，双击系统查看器打开
5. **`_get_ha_client(config)` 返回 `(url, headers, err)`**：复用现有连接逻辑（L448-454）
6. **项目临时目录约定**：`agent/tmp_dir.py` 提供 `save_to_tmp(filename, data) -> str`（保存 bytes 返回路径）、`cleanup_old_tmp()`（清理非当天文件，由 `niu_api/__main__.py` 每日定时调用）。ha-server 复用 `save_to_tmp`，带 fallback。
7. **mcp-servers.yaml 的 tools 列表是 visibility 覆盖，非白名单**：`mcp_loader.py` 的 `register_server()` 从 `module.get_tool_schemas()` 自动注册所有工具。未在 yaml 中列出的工具默认 `visibility=hidden`，仍可通过 `disk()` 被 Agent 调用。ha_automation/ha_scene/ha_script 三个工具均未在 yaml 中列出但正常工作。因此 **不需要修改 mcp-servers.yaml**。

## 临时目录策略

项目已有 `agent/tmp_dir.py` 模块（路径 `~/.niu/tmp/`），提供：
- `get_tmp_dir()` → 返回 `Path.home() / ".niu" / "tmp"`
- `save_to_tmp(filename, data)` → 保存 bytes，返回路径字符串
- `cleanup_old_tmp()` → 清理非当天文件（由 `niu_api/__main__.py` 每日定时调用）

ha_get_image 复用 `save_to_tmp` 函数，与 photo-server 一致。fallback 模式：
```python
try:
    from agent.tmp_dir import save_to_tmp
    filepath = save_to_tmp(filename, resp.content)
except ImportError:
    tmp_dir = os.path.join(os.path.expanduser("~"), ".niu", "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    filepath = os.path.join(tmp_dir, filename)
    with open(filepath, "wb") as f:
        f.write(resp.content)
```

**注意**：`cleanup_old_tmp()` 每日清理非当天文件。HA 地图图片是实时快照，次日文件被清理后，历史聊天中的图片路径会失效（裂图）。这是项目级设计约定（photo-server facebox 同样如此），非本工具引入的问题。

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `mcp-servers/ha-server/src/niu_ha_server/__init__.py` | 新增 `ha_get_image()` 函数 + `TOOL_SCHEMAS` 注册 | 修改 |
| `config/disk/ha-server.yaml` | MCP 虚拟磁盘映射 `ha/ha_get_image` | 修改 |
| `tests/test_ha_get_image.py` | 单元测试 + 真实 HA 集成测试 | 创建 |

---

### Task 1: ha_get_image 函数实现

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`（在 `ha_script` 函数之后、`run_server` 之前插入）
- Test: `tests/test_ha_get_image.py`

- [ ] **Step 1: 写失败测试（未配置 HA 时返回错误）**

创建 `tests/test_ha_get_image.py`：

```python
"""ha_get_image 工具测试 — 使用真实 HA 环境"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp-servers", "ha-server", "src"))
from niu_ha_server import ha_get_image, ha_setup, ha_status


def _ensure_connected():
    """确保 HA 已连接"""
    result = ha_setup()
    if not result.get("connected"):
        ha_url = os.environ.get("HA_URL", "http://localhost:8123")
        ha_token = os.environ.get("HA_TOKEN", "")
        if not ha_token:
            pytest.skip("HA_TOKEN not set")
        result = ha_setup(ha_url=ha_url, ha_token=ha_token)
        assert result.get("connected"), f"HA connection failed: {result}"


def _find_image_entity():
    """动态查询第一个 image 域实体，避免硬编码 entity_id"""
    _ensure_connected()
    status = ha_status(domain="image")
    if status.get("connected") and status.get("devices"):
        return status["devices"][0]["entity_id"]
    return None


class TestHaGetImage:
    def test_no_config_returns_error(self, monkeypatch):
        """未配置 HA 时返回连接错误（用 monkeypatch 避免操作真实配置文件）"""
        import niu_ha_server
        # mock _read_config 返回空字典，模拟未配置状态
        monkeypatch.setattr(niu_ha_server, "_read_config", lambda: {})
        result = ha_get_image(entity_id="image.test_map")
        assert not result.get("success", False)
        assert "error" in result

    def test_get_image_success(self):
        """成功下载 image 域图片"""
        entity_id = _find_image_entity()
        if not entity_id:
            pytest.skip("No image entity available")
        result = ha_get_image(entity_id=entity_id)
        try:
            assert result.get("success"), f"下载失败: {result}"
            assert "path" in result
            assert os.path.exists(result["path"])
            assert os.path.getsize(result["path"]) > 0
            expected_dir = os.path.expanduser("~/.niu/tmp")
            assert result["path"].startswith(expected_dir)
            assert "content_type" in result
            assert result["content_type"].startswith("image/")
            assert "size" in result
            assert result["size"] > 0
        finally:
            if result.get("path") and os.path.exists(result["path"]):
                os.remove(result["path"])

    def test_get_image_nonexistent_entity(self):
        """不存在的 entity_id 返回错误"""
        _ensure_connected()
        result = ha_get_image(entity_id="image.nonexistent_test_12345")
        assert not result.get("success", False)
        assert "error" in result

    def test_get_image_401_returns_auth_error(self, monkeypatch):
        """token 无效时返回 401 错误（提示重新 ha_setup）"""
        _ensure_connected()  # 先确认 HA 可达，不可达则 skip
        import niu_ha_server
        # 读取真实 ha_url，避免硬编码
        real_config = niu_ha_server._read_config()
        real_url = real_config.get("ha_url", "http://localhost:8123")
        # mock _read_config 返回无效 token（但用真实 ha_url）
        monkeypatch.setattr(niu_ha_server, "_read_config", lambda: {
            "ha_url": real_url,
            "ha_token": "invalid_token_for_test",
        })
        result = ha_get_image(entity_id="image.test_map")
        assert not result.get("success", False)
        assert "error" in result
        assert "401" in result["error"] or "认证" in result["error"]
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_ha_get_image.py::TestHaGetImage::test_no_config_returns_error -v`
Expected: FAIL with `ImportError: cannot import name 'ha_get_image'`

- [ ] **Step 3: 实现 ha_get_image 函数**

在 `mcp-servers/ha-server/src/niu_ha_server/__init__.py` 的 `ha_script` 函数之后（约 L2105，`run_server` 之前），插入：

```python
# --- ha_get_image ---


def ha_get_image(entity_id: str = "", **kwargs) -> dict:
    """下载 HA image 域图片实体到本地（扫地机地图等）。

    通过 HA REST API GET /api/image_proxy/{entity_id} 获取图片二进制，
    保存到 ~/.niu/tmp/ 目录，返回本地路径供 Agent 用 Markdown 展示。
    仅支持 image 域实体。camera 域需用 /api/camera_proxy 端点，暂不支持。

    Args:
        entity_id: HA image 域实体 ID，如 image.xxx_map

    Returns:
        {"success": True, "path": "/Users/.../.niu/tmp/xxx_map.svg",
         "content_type": "image/svg+xml", "size": 43293}
        或 {"success": False, "error": "..."}
    """
    if not entity_id:
        return {"success": False, "error": "entity_id 不能为空"}

    config = _read_config()
    url, headers, err = _get_ha_client(config)
    if err:
        return {"success": False, "error": "未配置 Home Assistant，请先使用 ha_setup 工具连接"}

    try:
        # image_proxy 不需要 Content-Type: application/json header
        img_headers = {"Authorization": headers["Authorization"]}
        resp = _requests.get(
            f"{url}/api/image_proxy/{entity_id}",
            headers=img_headers,
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code == 404:
            return {"success": False, "error": f"实体 {entity_id} 不存在或非 image 域图片类型"}
        if resp.status_code == 401:
            return {"success": False, "error": f"HA 认证失败（HTTP 401），请使用 ha_setup 重新配置 token"}
        if resp.status_code != 200:
            return {"success": False, "error": f"下载失败: HTTP {resp.status_code}"}

        content_type = resp.headers.get("content-type", "image/png").split(";")[0].strip()
        # 从 content-type 推断扩展名
        ext_map = {
            "image/svg+xml": ".svg",
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }
        ext = ext_map.get(content_type, ".png")

        # 生成文件名：entity_id 中的 . 替换为 _，加毫秒时间戳防覆盖
        safe_name = entity_id.replace(".", "_")
        filename = f"{safe_name}_{int(time.time() * 1000)}{ext}"

        # 复用 agent.tmp_dir.save_to_tmp（同进程架构下可导入）
        try:
            from agent.tmp_dir import save_to_tmp
            filepath = save_to_tmp(filename, resp.content)
        except ImportError:
            tmp_dir = os.path.join(os.path.expanduser("~"), ".niu", "tmp")
            os.makedirs(tmp_dir, exist_ok=True)
            filepath = os.path.join(tmp_dir, filename)
            with open(filepath, "wb") as f:
                f.write(resp.content)

        print(f"[HA] 下载图片: {entity_id} -> {filepath} ({len(resp.content)} bytes, {content_type})")

        return {
            "success": True,
            "path": filepath,
            "content_type": content_type,
            "size": len(resp.content),
        }
    except Exception as e:
        return {"success": False, "error": f"下载图片失败: {e}"}
```

注意：ha-server 使用 `print()` 做日志（项目中唯一不用 loguru 的 MCP server），不要用 `logger`。`time` 已在 L7 import。`os` 已在 L4 import。`_requests` 已在 L15 import。`_read_config` 和 `_get_ha_client` 是文件内已有函数。

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /Users/lilei/tools/ai-bot && python -m pytest tests/test_ha_get_image.py -v`
Expected: 5 PASSED（或部分 SKIP 如果 HA 未连接）

- [ ] **Step 5: ruff 检查**

Run: `cd /Users/lilei/tools/ai-bot && ruff check mcp-servers/ha-server/src/niu_ha_server/__init__.py`
Expected: OK

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py tests/test_ha_get_image.py
git commit -m "feat(ha): 新增 ha_get_image 工具下载图片实体到本地"
```

---

### Task 2: TOOL_SCHEMAS 注册

**Files:**
- Modify: `mcp-servers/ha-server/src/niu_ha_server/__init__.py`（TOOL_SCHEMAS 字典末尾，`ha_script` 之后）

- [ ] **Step 1: 在 TOOL_SCHEMAS 中添加 ha_get_image**

在 `mcp-servers/ha-server/src/niu_ha_server/__init__.py` 的 `TOOL_SCHEMAS` 字典中，`ha_script` 条目之后（字典 `}` 闭合之前），添加：

```python
    "ha_get_image": {
        "name": "ha_get_image",
        "description": "下载 HA image 域图片实体到本地（扫地机地图等），返回本地文件路径。Agent 可用 Markdown ![描述](路径) 在聊天中展示图片。entity_id 从 ha_status(domain='image') 获取。图片保存到 ~/.niu/tmp/ 目录。仅支持 image 域，不支持 camera 域。",
        "input_schema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "image 域实体 ID，如 image.xxx_map"},
            },
            "required": ["entity_id"],
        },
    },
```

- [ ] **Step 2: 验证 schema 注册**

Run: `cd /Users/lilei/tools/ai-bot && python -c "import sys; sys.path.insert(0, 'mcp-servers/ha-server/src'); from niu_ha_server import get_tool_schemas; schemas = {s['name']: s for s in get_tool_schemas()}; print('ha_get_image' in schemas); print(schemas.get('ha_get_image', {}).get('description', 'MISSING')[:60])"`
Expected: `True` + 描述前 60 字符

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add mcp-servers/ha-server/src/niu_ha_server/__init__.py
git commit -m "feat(ha): TOOL_SCHEMAS 注册 ha_get_image"
```

---

### Task 3: MCP 虚拟磁盘映射

**Files:**
- Modify: `config/disk/ha-server.yaml`（tools 列表末尾）

- [ ] **Step 1: 在 ha-server.yaml 的 tools 列表末尾添加 ha_get_image 映射**

在 `config/disk/ha-server.yaml` 的 `tools` 列表末尾（`ha_script` 之后），添加：

```yaml
  - name: ha_get_image
    category: write
    short: "下载图片到本地（地图）"
    long: "下载 HA image 域图片实体到本地文件，返回路径供展示。仅支持 image 域（扫地机地图等），不支持 camera 域。entity_id 从 ha_status(domain='image') 获取。返回路径可用 Markdown ![](路径) 展示"
    parameters:
      - name: entity_id
        position: 1
        type: string
        required: true
        description: "image 域实体 ID，如 image.xxx_map"
```

- [ ] **Step 2: 验证 YAML 语法**

Run: `cd /Users/lilei/tools/ai-bot && python -c "import yaml; yaml.safe_load(open('config/disk/ha-server.yaml')); print('YAML OK')"`
Expected: `YAML OK`

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add config/disk/ha-server.yaml
git commit -m "feat(ha): MCP 虚拟磁盘映射 ha_get_image"
```

---

### Task 4: 运行环境验证

**Files:** 无（手动验证）

- [ ] **Step 1: 重启应用**

```bash
cd /Users/lilei/tools/ai-bot && ./niu
```

- [ ] **Step 2: 在聊天中测试**

输入消息："帮我看看扫地机的地图"

预期行为：
1. Agent 调用 `ha_status(domain="image")` 找到 `image.18603118098_map`
2. Agent 调用 `ha_get_image(entity_id="image.18603118098_map")`
3. 工具返回 `{"success": true, "path": "/Users/.../.niu/tmp/18603118098_map_xxx.svg", ...}`
4. Agent 回复包含 `![扫地机地图](/Users/.../.niu/tmp/18603118098_map_xxx.svg)`
5. chat.html 渲染显示地图图片

- [ ] **Step 3: 验证图片显示**

- 聊天界面应显示扫地机地图 SVG 图片
- 图片有 `.chat-image` class（自适应缩放，最大 200x200）
- 双击图片应用系统查看器打开

- [ ] **Step 4: 验证 DevTools Console 无错误**

F12 打开 DevTools，Console 应无 `ERR_FILE_NOT_FOUND` 或其他错误

- [ ] **Step 5: 确认提交历史**

```bash
cd /Users/lilei/tools/ai-bot
git log --oneline -5
```
确认 3 个实现提交都在历史中。

---

## Self-Review

### 1. Spec coverage
- ✅ 下载 HA image 域图片实体到本地 → Task 1 `ha_get_image()` 函数
- ✅ 返回路径供 Agent 展示 → Task 1 返回 `path` 字段
- ✅ TOOL_SCHEMAS 注册 → Task 2
- ✅ MCP 虚拟磁盘映射 → Task 3
- ✅ 运行环境验证 → Task 4
- ✅ 仅支持 image 域（camera 域需 camera_proxy 端点，留作后续需求）
- ✅ 临时文件目录 `~/.niu/tmp/` → Task 1 复用 `agent.tmp_dir.save_to_tmp()` + fallback
- ✅ 不修改 mcp-servers.yaml → mcp_loader.py 自动注册 TOOL_SCHEMAS 中的所有工具（已验证）
- ✅ chat.html 图片显示已就绪 → 已验证 L1094-1118

### 2. Placeholder scan
- 无 TBD/TODO/"fill in details"
- 所有代码块完整
- 所有命令含 expected output

### 3. Type consistency
- `ha_get_image(entity_id: str)` → TOOL_SCHEMAS `entity_id: string` → disk yaml `entity_id: string` ✓
- 返回 `{"success": bool, "path": str, "content_type": str, "size": int}` 全链路一致 ✓
- 临时目录路径 `~/.niu/tmp/` 与 photo-server 一致 ✓
- category: write 与 photo-server ingest 一致 ✓
