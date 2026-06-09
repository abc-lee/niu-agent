# LightRAG 独立 LLM 模型配置实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Agent 的 LLM 配置和 LightRAG 知识图谱的 LLM 配置分离，允许两边使用不同的模型，解决思考链模型导致入库超时的问题。

**Architecture:** 在 `user-config.json` 中新增 `lightrag_llm` 配置段，与 `llm` 段结构对称。`llm_proxy.py` 根据请求是否来自 LightRAG（通过系统提示词中的 "Knowledge Graph Specialist" 标记判断），选择对应的配置段。此标记会匹配提取和摘要两种请求，两者都属于轻量级图谱操作，使用同一配置合理。`lightrag_llm` 不存在或 model 为空时回退到 `llm`；`lightrag_llm` 有 model 但缺 apiKey/apiBase 时从 `llm` 段继承。零配置变更即可运行。

**Tech Stack:** Python, FastAPI, LiteLLM

---

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `niu_api/llm_proxy.py` | LLM 代理端点，双模型配置路由 | 修改 |
| `config/user-config.json` | LLM 配置文件 | 修改 |
| `mcp-servers/config-manager/src/niu_config_manager/__init__.py` | 配置管理 MCP 工具 | 修改 |
| `tests/test_llm_proxy_config.py` | 双模型配置单元测试 | 新建 |
| `docs/manual-vector-store.md` | 操作手册 | 修改 |

不修改的文件（Agent 侧完全不变）：
- `agent/runner.py`
- `agent/generic/litellm_adapter.py`
- `niu_api/internal/lightrag_manager.py`
- `agent/subagent.py`

---

### Task 1: 改造 `get_llm_config()` 支持读取 `lightrag_llm` 配置段

**Files:**
- Modify: `niu_api/llm_proxy.py:198-219`
- Create: `tests/test_llm_proxy_config.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for llm_proxy dual-model configuration."""
import pytest
from unittest.mock import patch


def test_get_lightrag_config_reads_lightrag_llm_section():
    """When use_lightrag_config=True and lightrag_llm section exists, return it."""
    from niu_api.llm_proxy import get_llm_config

    mock_return = {"type": "openai", "apikey": "key2", "apibase": "https://api2.com", "model": "model2", "presetid": ""}
    with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_return) as mock_fn:
        # We need to test the actual function, not the mock. Use direct call with mocked file.
        pass

    # Direct approach: mock the file read by patching json.loads within the module
    import json
    mock_data = {
        "llm": {"apiKey": "key1", "apiBase": "https://api1.com", "model": "model1", "type": "openai"},
        "lightrag_llm": {"apiKey": "key2", "apiBase": "https://api2.com", "model": "model2", "type": "openai"},
    }
    with patch("builtins.open", create=True):
        with patch("json.loads", return_value=mock_data):
            config = get_llm_config(use_lightrag_config=True)

    assert config["apikey"] == "key2"
    assert config["model"] == "model2"


def test_get_lightrag_config_falls_back_to_llm():
    """When use_lightrag_config=True but lightrag_llm section is missing, fall back to llm."""
    from niu_api.llm_proxy import get_llm_config
    import json

    mock_data = {
        "llm": {"apiKey": "key1", "apiBase": "https://api1.com", "model": "model1", "type": "openai"},
    }
    with patch("builtins.open", create=True):
        with patch("json.loads", return_value=mock_data):
            config = get_llm_config(use_lightrag_config=True)

    assert config["apikey"] == "key1"
    assert config["model"] == "model1"


def test_get_lightrag_config_falls_back_when_lightrag_llm_empty_model():
    """When lightrag_llm section exists but has no model, fall back to llm."""
    from niu_api.llm_proxy import get_llm_config
    import json

    mock_data = {
        "llm": {"apiKey": "key1", "apiBase": "https://api1.com", "model": "model1", "type": "openai"},
        "lightrag_llm": {"apiKey": "key2", "apiBase": "https://api2.com", "model": "", "type": "openai"},
    }
    with patch("builtins.open", create=True):
        with patch("json.loads", return_value=mock_data):
            config = get_llm_config(use_lightrag_config=True)

    assert config["model"] == "model1"


def test_get_lightrag_config_inherits_apikey_from_llm():
    """When lightrag_llm has model but no apiKey, inherit from llm section."""
    from niu_api.llm_proxy import get_llm_config
    import json

    mock_data = {
        "llm": {"apiKey": "key1", "apiBase": "https://api1.com", "model": "model1", "type": "openai"},
        "lightrag_llm": {"apiBase": "https://api2.com", "model": "model2", "type": "openai"},
    }
    with patch("builtins.open", create=True):
        with patch("json.loads", return_value=mock_data):
            config = get_llm_config(use_lightrag_config=True)

    assert config["model"] == "model2"
    assert config["apikey"] == "key1"  # inherited from llm


def test_get_llm_config_default_unchanged():
    """When use_lightrag_config=False (default), always use llm section."""
    from niu_api.llm_proxy import get_llm_config
    import json

    mock_data = {
        "llm": {"apiKey": "key1", "apiBase": "https://api1.com", "model": "model1", "type": "openai"},
        "lightrag_llm": {"apiKey": "key2", "apiBase": "https://api2.com", "model": "model2", "type": "openai"},
    }
    with patch("builtins.open", create=True):
        with patch("json.loads", return_value=mock_data):
            config = get_llm_config(use_lightrag_config=False)

    assert config["apikey"] == "key1"
    assert config["model"] == "model1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_llm_proxy_config.py -v`
Expected: FAIL — `get_llm_config()` doesn't accept `use_lightrag_config` parameter

- [ ] **Step 3: Implement `get_llm_config()` with `use_lightrag_config` parameter**

Replace `niu_api/llm_proxy.py:198-219` with:

```python
def get_llm_config(use_lightrag_config: bool = False) -> Dict[str, str]:
    """Read LLM config from file.

    Args:
        use_lightrag_config: If True, read from 'lightrag_llm' section
            (falling back to 'llm' if not configured). If False, read from 'llm'.
            When lightrag_llm has model but missing apiKey/apiBase, those fields
            are inherited from the llm section.
    """
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})

        # If requesting lightrag config, try lightrag_llm section first
        if use_lightrag_config:
            lightrag_llm = data.get("lightrag_llm", {})
            # Only use lightrag_llm if it has a model configured
            if lightrag_llm.get("model"):
                # Inherit apiKey/apiBase from llm if missing in lightrag_llm
                if not lightrag_llm.get("apiKey"):
                    lightrag_llm["apiKey"] = llm.get("apiKey", "")
                if not lightrag_llm.get("apiBase"):
                    lightrag_llm["apiBase"] = llm.get("apiBase", "")
                if not lightrag_llm.get("type"):
                    lightrag_llm["type"] = llm.get("type", "openai")
                llm = lightrag_llm

        # 统一转换为小写键名
        config = {}
        for key, value in llm.items():
            config[key.lower()] = value

        config.setdefault("type", "openai")
        config.setdefault("apikey", "")
        config.setdefault("apibase", "")
        config.setdefault("model", "")

        return config
    except Exception:
        return {"type": "openai", "apikey": "", "apibase": "", "model": ""}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_llm_proxy_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/llm_proxy.py tests/test_llm_proxy_config.py
git commit -m "feat: add lightrag_llm config section support to get_llm_config"
```

---

### Task 2: 改造 `call_llm_via_litellm()` 和 `chat_completions()` 传递配置选择

**Files:**
- Modify: `niu_api/llm_proxy.py:222-337` (call_llm_via_litellm)
- Modify: `niu_api/llm_proxy.py:345-406` (chat_completions)

- [ ] **Step 1: Add `use_lightrag_config` parameter to `call_llm_via_litellm()`**

In `niu_api/llm_proxy.py`, change the function signature at line 222 from:

```python
async def call_llm_via_litellm(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    response_format: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
```

to:

```python
async def call_llm_via_litellm(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    response_format: Optional[Dict[str, Any]] = None,
    use_lightrag_config: bool = False,
) -> Dict[str, Any]:
```

Then at line 236, change:

```python
        config = get_llm_config()
```

to:

```python
        config = get_llm_config(use_lightrag_config=use_lightrag_config)
```

- [ ] **Step 2: Add LightRAG detection in `chat_completions()` and unify config usage**

In `niu_api/llm_proxy.py`, first add the import at the top of the file (after line 27 where `inject_brain_region_context` is imported):

```python
from niu_api.internal.brain_region_prompt import is_lightrag_extraction_request
```

Then restructure `chat_completions()` to detect LightRAG requests early and use a unified config for all operations. The current function calls `get_llm_config()` at line 361 for API key validation, then calls `call_llm_via_litellm()` at line 388 which internally calls `get_llm_config()` again, and uses `config["model"]` at line 395 for the response. All three must use the same config.

Change the `chat_completions()` function body as follows:

1. **After brain region injection** (around line 380), add LightRAG detection:
```python
        # Detect if this is a LightRAG extraction request for config routing
        is_lightrag = is_lightrag_extraction_request(litellm_messages)
```

2. **Replace the config read at line 361** — move it after the detection:
```python
        config = get_llm_config(use_lightrag_config=is_lightrag)
```
Note: This requires restructuring the function slightly. The current line 361 `config = get_llm_config()` is called before message conversion and brain region injection. Move it after the `is_lightrag` detection so it can pass the correct flag.

3. **Pass the flag to `call_llm_via_litellm`** (around line 388):
```python
        response = await call_llm_via_litellm(
            litellm_messages,
            tools=litellm_tools if litellm_tools else None,
            response_format=request.response_format,
            use_lightrag_config=is_lightrag,
        )
```

4. **Use the correct config for response** (around line 395) — the `config` variable read earlier with the correct flag should be used:
```python
        return litellm_to_openai_response(response, config["model"])
```

- [ ] **Step 3: Run syntax check**

Run: `python -m py_compile niu_api/llm_proxy.py`
Expected: No output (success)

- [ ] **Step 4: Run existing tests**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_llm_proxy_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add niu_api/llm_proxy.py
git commit -m "feat: route LightRAG requests to lightrag_llm config in llm_proxy"
```

---

### Task 3: 添加 config-manager MCP 工具支持 `lightrag_llm` 配置

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:395-443`

- [ ] **Step 1: Add `get_lightrag_llm_config` tool**

In `mcp-servers/config-manager/src/niu_config_manager/__init__.py`, after the existing `get_llm_config` function (around line 405), add:

```python
def get_lightrag_llm_config() -> dict[str, Any]:
    """Get LightRAG LLM configuration (without API key for security).

    Returns the lightrag_llm section if configured, otherwise indicates
    it will fall back to the llm section.
    """
    config = load_user_config()
    lightrag_llm = config.get("lightrag_llm", {})
    return {
        "presetId": lightrag_llm.get("presetId", ""),
        "apiBase": lightrag_llm.get("apiBase", ""),
        "model": lightrag_llm.get("model", ""),
        "type": lightrag_llm.get("type", "openai"),
        "hasApiKey": bool(lightrag_llm.get("apiKey", "")),
        "configured": bool(lightrag_llm.get("model", "")),
    }
```

- [ ] **Step 2: Add `set_lightrag_llm_config` tool**

After the existing `set_llm_config` function (around line 443), add:

```python
def set_lightrag_llm_config(
    preset_id: str = None,
    api_key: str = None,
    api_base: str = None,
    model: str = None,
    llm_type: str = None,
) -> dict[str, Any]:
    """Set LightRAG LLM configuration.

    If model is set to empty string, removes the lightrag_llm section
    so that LightRAG falls back to the main llm configuration.
    """
    config = load_user_config()

    # If clearing the config (model=""), remove the section
    if model == "":
        config.pop("lightrag_llm", None)
        save_user_config(config)
        return {"status": "cleared", "message": "LightRAG will use main LLM config"}

    lightrag_llm = config.get("lightrag_llm", {})

    # If preset_id is provided, load from presets
    if preset_id:
        presets = load_presets()
        for preset in presets:
            if preset.get("id") == preset_id:
                lightrag_llm["presetId"] = preset_id
                lightrag_llm["apiBase"] = preset.get("apiBase", "")
                lightrag_llm["model"] = preset.get("model", "")
                lightrag_llm["type"] = preset.get("type", "openai")
                break

    # Override with explicit values
    if api_key is not None:
        lightrag_llm["apiKey"] = api_key
    if api_base is not None:
        lightrag_llm["apiBase"] = api_base
    if model is not None:
        lightrag_llm["model"] = model
    if llm_type is not None:
        lightrag_llm["type"] = llm_type

    config["lightrag_llm"] = lightrag_llm
    save_user_config(config)

    return {"status": "updated", "lightrag_llm": get_lightrag_llm_config()}
```

- [ ] **Step 3: Register tools in TOOL_SCHEMAS, `call_tool()`, and `list_tools()`**

Three places must be updated in `mcp-servers/config-manager/src/niu_config_manager/__init__.py`:

**A. TOOL_SCHEMAS dict** — Add `get_lightrag_llm_config` and `set_lightrag_llm_config` entries following the pattern of the existing `get_llm_config` / `set_llm_config` entries. `get_lightrag_llm_config` has no input parameters. `set_lightrag_llm_config` has the same parameters as `set_llm_config` (preset_id, api_key, api_base, model, llm_type).

**B. `call_tool()` function** (around line 1071-1163) — Add `elif` branches for the new tool names:
```python
        elif tool_name == "get_lightrag_llm_config":
            return get_lightrag_llm_config()
        elif tool_name == "set_lightrag_llm_config":
            return set_lightrag_llm_config(**arguments)
```

**C. `list_tools()` function** (around line 824-1068) — Add `Tool(...)` entries for the new tools so MCP clients can discover them.

- [ ] **Step 4: Run syntax check**

Run: `python -m py_compile mcp-servers/config-manager/src/niu_config_manager/__init__.py`
Expected: No output (success)

- [ ] **Step 5: Commit**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py
git commit -m "feat: add get/set_lightrag_llm_config MCP tools"
```

---

### Task 4: 更新操作手册

**Files:**
- Modify: `docs/manual-vector-store.md`

- [ ] **Step 1: Add lightrag_llm configuration section to the manual**

In `docs/manual-vector-store.md`, after the existing "8.5 LightRAG 入库配置" section, add a new subsection or extend the existing one with:

```markdown
#### LightRAG 独立模型配置

LightRAG 入库可使用与主 Agent 不同的模型。官方建议入库时不要使用带思考链的模型，
因为深度推理会显著拖慢实体提取速度甚至导致超时。

在 `config/user-config.json` 中新增 `lightrag_llm` 配置段：

\`\`\`json
{
  "llm": {
    "model": "ark-code-latest",
    "apiKey": "...",
    "apiBase": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "type": "openai"
  },
  "lightrag_llm": {
    "model": "doubao-pro-32k",
    "apiKey": "...",
    "apiBase": "https://ark.cn-beijing.volces.com/api/v3",
    "type": "openai"
  }
}
\`\`\`

- `lightrag_llm` 段不存在或 `model` 为空时，自动回退到 `llm` 段
- 两个配置段使用相同的字段结构（model、apiKey、apiBase、type）
- 修改配置后重启程序生效
- 也可通过 MCP 工具 `set_lightrag_llm_config` 动态修改
```

- [ ] **Step 2: Commit**

```bash
git add docs/manual-vector-store.md
git commit -m "docs: add lightrag_llm config section to vector store manual"
```

---

### Task 5: 更新 `user-config.json` 添加 `lightrag_llm` 示例

**Files:**
- Modify: `config/user-config.json`

- [ ] **Step 1: Add empty `lightrag_llm` section**

Update `config/user-config.json` to include the new section with placeholder values:

```json
{
  "llm": {
    "presetId": "ark-code-latest",
    "apiKey": "REDACTED_VOLCES_API_KEY",
    "apiBase": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "model": "ark-code-latest",
    "type": "openai"
  },
  "lightrag_llm": {
  }
}
```

注意：`lightrag_llm` 段留空，因为 model 为空会自动回退到 `llm` 段，确保现有用户升级后行为不变。用户需要自行填入独立的模型配置。

- [ ] **Step 2: Commit**

```bash
git add config/user-config.json
git commit -m "feat: add empty lightrag_llm section to user-config.json"
```

---

## Verification

1. **回退兼容测试**：不填 `lightrag_llm` 段或 model 为空 → 应回退到 `llm` 段，行为与当前一致
2. **双模型路由测试**：配置 `lightrag_llm` 为不同模型 → Agent 聊天用 `llm` 模型，LightRAG 入库用 `lightrag_llm` 模型
3. **脑区注入测试**：配置双模型后，LightRAG 入库时脑区上下文注入仍正常工作
4. **入库基准测试**：用 SYSTEM_MANUAL.md 清空后入库，对比配置独立模型前后的耗时
