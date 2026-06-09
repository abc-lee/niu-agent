# LightRAG 思考链深度控制 + 双模型配置 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 LightRAG 入库请求提供思考链深度控制和独立模型配置，解决思考链模型导致入库超时的问题。核心功能是 `reasoning_effort` 参数从配置到 LLM 调用的完整传递链路。

**Architecture:** 在 `user-config.json` 新增 `lightrag_llm` 配置段，支持 `model`（独立模型）和 `reasoning_effort`（思考链深度）。`llm_proxy.py` 检测 LightRAG 请求后读取该段配置，将 `reasoning_effort` 注入 `LiteLLMSession`。`get_provider_params` 扩展为所有支持该参数的模型传递 `reasoning_effort`。默认值 `reasoning_effort: "none"` 确保即使用户配置了思考链模型，LightRAG 入库也不受影响。

**Tech Stack:** Python, LiteLLM, FastAPI

---

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `niu_api/llm_proxy.py` | LLM 代理端点，双模型配置路由 + reasoning_effort 传递 | 修改 |
| `agent/generic/litellm_adapter.py` | get_provider_params 扩展，所有模型支持 reasoning_effort | 修改 |
| `config/user-config.json` | LLM 配置文件，新增 lightrag_llm 段 | 修改 |
| `mcp-servers/config-manager/src/niu_config_manager/__init__.py` | 配置管理 MCP 工具 | 修改 |
| `docs/manual-vector-store.md` | 操作手册 | 修改 |

不修改的文件：
- `agent/generic/llmcore.py` — BaseSession 已有 reasoning_effort 解析逻辑，无需改动
- `agent/runner.py` — 主 Agent 配置链路不变
- `niu_api/internal/lightrag_manager.py` — 仍走 llm_proxy 代理
- `niu_api/internal/brain_region_prompt.py` — is_lightrag_extraction_request 已存在

---

### Task 1: 扩展 `get_provider_params` 支持所有模型的 reasoning_effort

**Files:**
- Modify: `agent/generic/litellm_adapter.py:218-235`

**背景**：当前 `get_provider_params` 只在模型名包含 "deepseek" 时传递 `reasoning_effort`。但 OpenAI o-series、火山方舟/豆包等模型也支持该参数。LiteLLM 将 `reasoning_effort` 作为 OpenAI 标准参数直接传递给底层 API，所以只需让 `get_provider_params` 对所有模型都传递该参数即可。

- [ ] **Step 1: 修改 `get_provider_params`**

将 `agent/generic/litellm_adapter.py` 中的 `get_provider_params` 函数替换为：

```python
def get_provider_params(model: str, reasoning_effort: Optional[str] = None) -> Dict[str, Any]:
    """获取提供商特定参数"""
    params: Dict[str, Any] = {}
    model_lower = model.lower()

    # Claude: 启用prompt caching
    if "claude" in model_lower:
        params["extra_headers"] = {"anthropic-beta": "prompt-caching-2024-07-31"}

    # reasoning_effort: 支持 OpenAI o-series, DeepSeek, 火山方舟等模型
    # LiteLLM 将此参数作为 OpenAI 标准参数传递；不支持该参数的模型会被 LiteLLM 的 drop_params 忽略
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort

    return params
```

改动说明：
- 移除 DeepSeek 专属判断，改为所有模型都传递 `reasoning_effort`
- 移除 MiniMax 注释掉的代码（已无用）
- LiteLLM 会将 `reasoning_effort` 作为标准参数传递；对于不支持该参数的模型，设置 `drop_params=True` 可自动忽略（当前 `litellm.completion()` 默认行为：不支持的参数会报错，所以需要额外处理）

**重要补充**：检查 `litellm.completion()` 调用是否设置了 `drop_params`。如果没有，对于不支持 `reasoning_effort` 的模型（如普通 GPT-4、Claude Sonnet），传递该参数会导致 API 报错。

- [ ] **Step 2: 在 `LiteLLMSession.chat()` 中有条件添加 `drop_params=True`**

`drop_params=True` 会让 LiteLLM 静默丢弃目标 API 不支持的参数。如果全局启用，主 Agent 聊天路径中的参数拼写错误也会被静默忽略，改变了错误报告行为。因此只在传递了 `reasoning_effort` 时才启用。

在 `litellm_adapter.py` 的 `chat()` 方法中，`request_params` 字典构建之后，有条件添加：

```python
        request_params: Dict[str, Any] = {
            "model": self.default_model,
            "messages": messages,
            "stream": True,
            "custom_llm_provider": custom_provider,
            "api_base": self.api_base or None,
            "api_key": self.api_key or None,
            "timeout": 120,
            **provider_params,
        }
        # Only drop unsupported params when passing reasoning_effort
        # (e.g., some models don't support this OpenAI extension parameter)
        if provider_params.get("reasoning_effort"):
            request_params["drop_params"] = True
```

- [ ] **Step 3: 语法检查**

Run: `python -m py_compile agent/generic/litellm_adapter.py`
Expected: No output (success)

- [ ] **Step 4: Commit**

```bash
git add agent/generic/litellm_adapter.py
git commit -m "feat: extend get_provider_params to pass reasoning_effort for all models; add drop_params=True"
```

---

### Task 2: 改造 `llm_proxy.py` 支持双模型配置 + reasoning_effort 传递

**Files:**
- Modify: `niu_api/llm_proxy.py:198-219` (get_llm_config)
- Modify: `niu_api/llm_proxy.py:222-253` (call_llm_via_litellm)
- Modify: `niu_api/llm_proxy.py:345-406` (chat_completions)

- [ ] **Step 1: 改造 `get_llm_config()` 支持 `lightrag_llm` 配置段**

替换 `niu_api/llm_proxy.py:198-219` 为：

```python
def get_llm_config(use_lightrag_config: bool = False) -> Dict[str, str]:
    """Read LLM config from file.

    Args:
        use_lightrag_config: If True, read from 'lightrag_llm' section.
            model 为空时使用主 llm 同一模型（正常默认行为）。
            apiKey/apiBase/type 为空时从 llm 段继承。
            reasoning_effort 默认 "none"（独立于模型配置，强制禁用思考链）。
            用户可在 lightrag_llm 段显式设置 reasoning_effort 覆盖默认值。
    """
    from pathlib import Path

    config_path = Path(__file__).parent.parent / "config" / "user-config.json"
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        llm = data.get("llm", {})

        # If requesting lightrag config, apply lightrag_llm overrides
        if use_lightrag_config:
            lightrag_llm = data.get("lightrag_llm", {})
            if lightrag_llm.get("model"):
                # Independent model configured: inherit missing fields from llm
                if not lightrag_llm.get("apiKey"):
                    lightrag_llm["apiKey"] = llm.get("apiKey", "")
                if not lightrag_llm.get("apiBase"):
                    lightrag_llm["apiBase"] = llm.get("apiBase", "")
                if not lightrag_llm.get("type"):
                    lightrag_llm["type"] = llm.get("type", "openai")
                # Default reasoning_effort to "none" if not explicitly set
                if not lightrag_llm.get("reasoning_effort"):
                    lightrag_llm["reasoning_effort"] = "none"
                llm = lightrag_llm
            else:
                # Use main llm model, but independently apply reasoning_effort
                # model 和 reasoning_effort 是两个独立维度
                llm = dict(llm)
                user_effort = lightrag_llm.get("reasoning_effort")
                llm["reasoning_effort"] = user_effort if user_effort else "none"

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
        return {"type": "openai", "apikey": "", "apibase": "", "model": "", "reasoning_effort": "none"}
```

关键设计：
- `reasoning_effort` 是独立于模型的配置维度，默认 `"none"`，零配置即生效
- 即使使用同一模型（model 为空），LightRAG 也能独立控制思考深度
- 用户可在 `lightrag_llm` 段显式设置 `reasoning_effort` 覆盖默认值（如 `"low"` 允许浅层推理）

- [ ] **Step 2: 改造 `call_llm_via_litellm()` 接受预加载 config（避免重复读文件）**

修改函数签名（第222行），接受预加载的 config dict，避免 `chat_completions` 和 `call_llm_via_litellm` 各读一次 `user-config.json`：

```python
async def call_llm_via_litellm(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    response_format: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
```

修改第236行的 `get_llm_config` 调用，改为使用传入的 config：

```python
    if config is None:
        config = get_llm_config()
```

修改第245-250行的 `llm_config` 构造，传递 `reasoning_effort`：

```python
    llm_config = {
        "api_type": config.get("type", "openai"),
        "apikey": config["apikey"],
        "apibase": config["apibase"],
        "model": config["model"],
        "reasoning_effort": config.get("reasoning_effort"),
    }
```

这样 `BaseSession.__init__` 会从 `cfg["reasoning_effort"]` 读取并验证该值。
`chat_completions` 只读一次 `get_llm_config()`，将结果传给 `call_llm_via_litellm(config=config)`。

- [ ] **Step 3: 改造 `chat_completions()` 检测 LightRAG 请求并路由**

在 `niu_api/llm_proxy.py` 顶部添加 import（第27行附近，与 `inject_brain_region_context` 同一行）：

```python
from niu_api.internal.brain_region_prompt import inject_brain_region_context, is_lightrag_extraction_request
```

修改 `chat_completions()` 函数体：

1. 在脑区注入之前检测 is_lightrag（比注入后检测更清晰）：
```python
    # Detect if this is a LightRAG extraction request for config routing
    is_lightrag = is_lightrag_extraction_request(litellm_messages)
```

2. 将 config 读取移到检测之后：
```python
    config = get_llm_config(use_lightrag_config=is_lightrag)
```

注意：当前代码在第361行先读 config 做 API key 检查，然后第376行注入脑区，然后第388行调用 LLM。需要将 API key 检查和 config 读取移到脑区注入和 is_lightrag 检测之后。

3. 传递预加载的 config 给 `call_llm_via_litellm`（避免重复读文件）：
```python
    response = await call_llm_via_litellm(
        messages=litellm_messages,
        tools=litellm_tools,
        response_format=request.response_format,
        config=config,
    )
```

4. 使用已读取的 config 构建响应：
```python
    openai_response = litellm_to_openai_response(response, config["model"])
```

完整的 `chat_completions` 函数修改后的结构：

```python
@router.post("/chat/completions")
async def chat_completions(request: OpenAIChatRequest) -> OpenAIChatResponse:
    """OpenAI-compatible chat completions endpoint"""
    logger.info(f"[LLM Proxy] Received request: model={request.model}, messages={len(request.messages)}")
    logger.info(f"[LLM Proxy] Tools count: {len(request.tools) if request.tools else 0}")
    if request.tools:
        logger.info(f"[LLM Proxy] Tool names: {[t.function.get('name') for t in request.tools if hasattr(t, 'function')]}")

    # Convert OpenAI format to LiteLLM format
    litellm_messages = openai_to_litellm_messages(request.messages)
    litellm_tools = openai_to_litellm_tools(request.tools)

    # Detect LightRAG request BEFORE brain region injection
    is_lightrag = is_lightrag_extraction_request(litellm_messages)

    # Read LLM config (routes to lightrag_llm section if LightRAG request)
    config = get_llm_config(use_lightrag_config=is_lightrag)
    if not config["apikey"]:
        raise HTTPException(
            status_code=500,
            detail="LLM not configured. Please set API key in config/user-config.json"
        )

    # Inject brain region context for LightRAG extraction requests
    try:
        _t0 = time.time()
        litellm_messages = await asyncio.to_thread(inject_brain_region_context, litellm_messages)
        _t1 = time.time()
        logger.info(f"[LLM Proxy] Brain region injection took {_t1-_t0:.3f}s")
    except Exception:
        logger.warning("Brain region injection failed, continuing without it", exc_info=True)

    logger.debug(f"[LLM Proxy] Converted {len(litellm_messages)} messages")
    if litellm_tools:
        logger.debug(f"[LLM Proxy] Tools: {len(litellm_tools)}")

    # Log routing decision
    if is_lightrag:
        logger.info(f"[LLM Proxy] LightRAG request: model={config['model']}, reasoning_effort={config.get('reasoning_effort', 'N/A')}")

    # Call LLM (pass pre-loaded config to avoid double file read)
    try:
        response = await call_llm_via_litellm(
            messages=litellm_messages,
            tools=litellm_tools,
            response_format=request.response_format,
            config=config,
        )

        openai_response = litellm_to_openai_response(response, config["model"])

        logger.info(f"[LLM Proxy] Response: {len(openai_response.choices)} choices")
        return openai_response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[LLM Proxy] Error: {e}")
        import traceback
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
```

- [ ] **Step 4: 语法检查**

Run: `python -m py_compile niu_api/llm_proxy.py`
Expected: No output (success)

- [ ] **Step 5: Commit**

```bash
git add niu_api/llm_proxy.py
git commit -m "feat: lightrag_llm config routing + reasoning_effort pass-through in llm_proxy"
```

---

### Task 3: 添加 config-manager MCP 工具支持 `lightrag_llm` 配置

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py`

- [ ] **Step 1: 在 TOOL_SCHEMAS 添加两个新条目**

在 `test_llm_connection` 条目之后添加：

```python
    "get_lightrag_llm_config": {
        "name": "get_lightrag_llm_config",
        "description": "Get LightRAG LLM configuration (without API key for security). Returns the lightrag_llm section if configured, otherwise indicates it will fall back to the llm section.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    "set_lightrag_llm_config": {
        "name": "set_lightrag_llm_config",
        "description": "Set LightRAG LLM configuration. If model is set to empty string, removes the lightrag_llm section so that LightRAG falls back to the main LLM configuration. Default reasoning_effort is 'none' (disables thinking chain).",
        "input_schema": {
            "type": "object",
            "properties": {
                "preset_id": {
                    "type": "string",
                    "description": "Preset ID to load for LightRAG LLM",
                },
                "api_key": {"type": "string", "description": "API key (inherits from main llm if not set)"},
                "api_base": {"type": "string", "description": "API base URL (inherits from main llm if not set)"},
                "model": {"type": "string", "description": "Model name (empty string to clear and fall back to main llm)"},
                "llm_type": {
                    "type": "string",
                    "description": "Provider type: 'openai' or 'anthropic'",
                },
                "reasoning_effort": {
                    "type": "string",
                    "description": "Thinking chain depth: 'none' (default for LightRAG), 'low', 'medium', 'high'. LightRAG officially recommends 'none' to avoid timeouts.",
                },
            },
        },
    },
```

- [ ] **Step 2: 添加 `get_lightrag_llm_config` 和 `set_lightrag_llm_config` 函数**

在 `set_llm_config` 函数之后添加：

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
        "reasoning_effort": lightrag_llm.get("reasoning_effort", "none"),
    }


def set_lightrag_llm_config(
    preset_id: str = None,
    api_key: str = None,
    api_base: str = None,
    model: str = None,
    llm_type: str = None,
    reasoning_effort: str = None,
) -> dict[str, Any]:
    """Set LightRAG LLM configuration.

    If model is set to empty string, removes the lightrag_llm section
    so that LightRAG falls back to the main llm configuration.
    """
    config = load_user_config()

    # If clearing the model (model=""), remove model-specific fields
    # but preserve reasoning_effort (model 和 reasoning_effort 是独立维度)
    if model == "":
        lightrag_llm = config.get("lightrag_llm", {})
        for key in ("presetId", "apiKey", "apiBase", "model", "type"):
            lightrag_llm.pop(key, None)
        if lightrag_llm:
            config["lightrag_llm"] = lightrag_llm
        else:
            config.pop("lightrag_llm", None)
        save_user_config(config)
        return {"status": "cleared", "message": "LightRAG model cleared, will use main LLM model"}

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
    if reasoning_effort is not None:
        lightrag_llm["reasoning_effort"] = reasoning_effort

    config["lightrag_llm"] = lightrag_llm
    save_user_config(config)

    return {"status": "updated", "lightrag_llm": get_lightrag_llm_config()}
```

- [ ] **Step 3: 在 `list_tools()` 添加两个 Tool 条目**

在 `test_llm_connection` 的 Tool 条目之后添加：

```python
        Tool(
            name="get_lightrag_llm_config",
            description="Get LightRAG LLM configuration. Returns model, reasoning_effort, and whether it falls back to main llm.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_lightrag_llm_config",
            description="Set LightRAG LLM configuration. If model='', clears the section (falls back to main llm). Default reasoning_effort='none' disables thinking chain.",
            inputSchema={
                "type": "object",
                "properties": {
                    "preset_id": {
                        "type": "string",
                        "description": "Preset ID to load for LightRAG LLM",
                    },
                    "api_key": {"type": "string", "description": "API key (inherits from main llm if not set)"},
                    "api_base": {"type": "string", "description": "API base URL (inherits from main llm if not set)"},
                    "model": {"type": "string", "description": "Model name (empty string to clear)"},
                    "llm_type": {
                        "type": "string",
                        "description": "Provider type: 'openai' or 'anthropic'",
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": "Thinking chain depth: 'none', 'low', 'medium', 'high'. Default 'none'.",
                    },
                },
            },
        ),
```

- [ ] **Step 4: 在 `call_tool()` 添加 dispatch 分支**

在 `test_llm_connection` 分支之后添加：

```python
        elif name == "get_lightrag_llm_config":
            result = get_lightrag_llm_config()
        elif name == "set_lightrag_llm_config":
            result = set_lightrag_llm_config(
                preset_id=arguments.get("preset_id"),
                api_key=arguments.get("api_key"),
                api_base=arguments.get("api_base"),
                model=arguments.get("model"),
                llm_type=arguments.get("llm_type"),
                reasoning_effort=arguments.get("reasoning_effort"),
            )
```

- [ ] **Step 5: 语法检查**

Run: `python -m py_compile mcp-servers/config-manager/src/niu_config_manager/__init__.py`
Expected: No output (success)

- [ ] **Step 6: Commit**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py
git commit -m "feat: add get/set_lightrag_llm_config MCP tools with reasoning_effort support"
```

---

### Task 4: 更新配置文件和手册

**Files:**
- Modify: `config/user-config.json`
- Modify: `docs/manual-vector-store.md`

- [ ] **Step 1: 更新 `user-config.json` 添加完整的 `lightrag_llm` 配置模板**

JSON 不支持注释，所以用空字符串作为占位符，用户一看就知道该填什么。配置文件随打包程序分发，必须让用户无需猜测字段含义。

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
    "presetId": "",
    "apiKey": "",
    "apiBase": "",
    "model": "",
    "type": "openai",
    "reasoning_effort": "none"
  }
}
```

字段说明（与 `llm` 段对称，新增 `reasoning_effort`）：

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `presetId` | 预设ID（对应 llm-presets.json 中的 id，如 "doubao"） | 空 |
| `apiKey` | API密钥。为空时自动继承 `llm` 段的 apiKey | 空（继承 llm） |
| `apiBase` | API地址。为空时自动继承 `llm` 段的 apiBase | 空（继承 llm） |
| `model` | 模型名称。为空时使用主 Agent 同一模型（正常默认行为，不是"回退"） | 空（使用主模型） |
| `type` | 接口类型，"openai" 或 "anthropic" | "openai" |
| `reasoning_effort` | **思考链深度（核心配置）**："none"(禁用)、"low"、"medium"、"high" | "none" |

**设计要点**：
- `model` 和 `reasoning_effort` 是**两个独立的配置维度**，互不依赖
- 即使使用同一模型（model 为空），`reasoning_effort` 也必须独立控制
- `lightrag_llm` 段最精简的有效配置就是 `{"reasoning_effort": "none"}`，其余全继承
- 用户只需改 `model` 即可切换模型，只需改 `reasoning_effort` 即可调整思考深度
- 两者都改 = 独立模型 + 独立思考深度

- [ ] **Step 2: 在手册中添加"LightRAG 思考链与模型配置"章节**

在 `docs/manual-vector-store.md` 的 8.5 节之后（`### 8.6 向量模型切换` 之前），插入：

```markdown

#### LightRAG 思考链与模型配置

LightRAG 入库请求默认禁用思考链（`reasoning_effort: "none"`），防止深度推理导致实体提取超时。
LightRAG 官方明确建议不要使用带思考链的模型做入库。

**方案一：自动禁用思考链（零配置生效）**

不做任何配置，系统自动将所有 LightRAG 入库请求的 `reasoning_effort` 设为 `"none"`。
即使主 Agent 使用带思考链的模型（如 ark-code-latest），入库请求也不受影响。

**方案二：独立模型配置**

在 `config/user-config.json` 中配置 `lightrag_llm` 段，让 LightRAG 使用不同模型：

```json
{
  "llm": {
    "presetId": "ark-code-latest",
    "apiKey": "...",
    "apiBase": "https://ark.cn-beijing.volces.com/api/coding/v3",
    "model": "ark-code-latest",
    "type": "openai"
  },
  "lightrag_llm": {
    "presetId": "doubao",
    "apiKey": "",
    "apiBase": "https://ark.cn-beijing.volces.com/api/v3",
    "model": "doubao-pro-32k",
    "type": "openai",
    "reasoning_effort": "none"
  }
}
```

- `lightrag_llm` 段 `model` 为空时，使用主 Agent 同一模型（正常默认行为），但独立控制 `reasoning_effort`
- `lightrag_llm` 有 `model` 但缺 `apiKey`/`apiBase` 时，从 `llm` 段继承
- `reasoning_effort` 是独立配置维度，默认 `"none"`（禁用思考链），即使同一模型也可用不同思考深度
- 修改配置后重启程序生效
- 也可通过 MCP 工具 `set_lightrag_llm_config` 动态修改

**reasoning_effort 参数**

| 值 | 效果 | 适用场景 |
|----|------|----------|
| `none` | 完全禁用思考链（默认） | LightRAG 入库、简单提取任务 |
| `low` | 浅层推理 | 需要少量推理的入库任务 |
| `medium` | 中等推理 | 非入库的图谱查询任务 |
| `high` | 深度推理 | 不建议用于 LightRAG |

在 `lightrag_llm` 段中设置 `reasoning_effort` 可覆盖默认值 `"none"`。
```

- [ ] **Step 3: Commit**

```bash
git add docs/manual-vector-store.md
git commit -m "docs: add LightRAG thinking chain and model config section to manual"
```

---

## 已知遗留问题（本次不做，后续单独处理）

1. **`/llm/v1/models` 和 `/llm/v1/status` 端点不反映 lightrag_llm 模型** — 当前只返回主 `llm` 模型信息。LightRAG 初始化时不调用 `/models`（只用 `chat.completions.create`），因此不影响功能。但配置了独立 LightRAG 模型后，诊断端点无法反映，运维排查时可能困惑。后续需修改 status 端点增加 `lightrag_llm_model` 字段。
2. **`user-config.json` 读写无并发保护** — config-manager MCP 工具的读-修改-写操作无锁保护，并发调用可能导致丢失更新。这是预存问题（非本次引入），后续需加 `threading.Lock` 保护。
3. **DeepSeek reasoning_effort 行为变化** — 之前 `get_provider_params` 只对 DeepSeek 模型传递 `reasoning_effort`，改为所有模型都传递。对 DeepSeek 用户无功能变化；对切换了非 DeepSeek 模型且配置了 `reasoning_effort` 的用户，之前被忽略的值现在会发送到 API 并被 `drop_params` 丢弃。行为变化是静默的但无害。
3. **`call_llm_via_litellm` 其他调用方** — `agent/mcp_client.py:141` 也直接调用 `call_llm_via_litellm()`。该调用是 MCP Sampling 回调，用于 LLM 完成任务（如文档分类），与 LightRAG 提取无关，因此回退到主 `llm` 配置是正确行为。不传 `config` 时自动回退到 `get_llm_config()` 读主 llm 段，不含 reasoning_effort，这正是 MCP Sampling 所需。

---

## Verification

1. **思考链禁用验证**：不配置 `lightrag_llm` 段 → LightRAG 入库请求应带 `reasoning_effort="none"`，观察 LLM 响应无 thinking chain
2. **双模型路由验证**：配置 `lightrag_llm` 为不同模型 → Agent 聊天用 `llm` 模型，LightRAG 入库用 `lightrag_llm` 模型
3. **日志验证**：入库时观察 `[LLM Proxy] LightRAG request: model=xxx, reasoning_effort=xxx` 日志
4. **脑区注入验证**：配置双模型后，LightRAG 入库时脑区上下文注入仍正常工作
5. **入库基准测试**：用 SYSTEM_MANUAL.md 清空后入库，对比配置独立模型前后的耗时
