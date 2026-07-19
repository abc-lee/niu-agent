# LightRAG response_format 能力自动探测实施方案 v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在设置窗口测试连接通过后，自动递进探测当前 LLM 配置对 `response_format` 的支持档位（`json_schema` strict > `json_object` > prompt-only），将结果写入 `config/user-config.json` 的 `lightrag_llm.litellm_kwargs.response_format_mode`，避免知识图谱生成时因 response_format 失效（豆包网关 400 拒绝、GLM 接受但输出漂移）而报错或静默降级。

**Architecture:** 新建**独立端点** `/api/probe-response-format`（不修改 `/api/test-llm`，避免污染启动器复用的连通性测试）。该端点**直接复用 `LiteLLMSession.chat`** 调用路径（不绕过），按 3 档递进探测：先测 `json_schema` strict，失败再测 `json_object`，都失败则判定 `prompt_only`。判定**不仅看是否抛异常**，更要看**响应内容是否合法 JSON + 含目标字段**——因为实测发现 GLM 接受 response_format 参数但不真正遵守 schema，输出仍漂移导致 `json.loads` 失败。前端设置窗口在 `/api/test-llm` 通过后单独调一次探测端点，根据结果修改 `lightrag_llm.litellm_kwargs` 后 saveConfig。运行时 LightRAG `_llm_model_func` 在 `keyword_extraction=True` 时根据 `response_format_mode` 决定构造哪种 response_format。**升级后首次启动**若检测到旧配置无 `response_format_mode`，后台自动触发一次探测。

**Tech Stack:** Python（FastAPI/LiteLLM/litellm.BadRequestError/litellm.UnsupportedParamsError）、原生 JS（Electron 设置窗口）、pytest（含真实 LLM 调用的集成测试）、TDD。

---

## 真实环境验证结论（2026-07-19 实测）

详见 `docs/superpowers/plans/2026-07-19-response-format-real-env-verification.md`。

**关键事实**（推翻 v1 假设）：

1. **豆包 Coding Plan 端点**（`config/user-config.json`，model=`ark-code-latest`）：
   - `drop_params=False`：LiteLLM 客户端 volcengine router 抛 `UnsupportedParamsError`，请求未发出
   - `drop_params=True` + `allowed_openai_params=["response_format"]`：LiteLLM 真正透传 response_format，豆包网关返回 `BadRequestError`："response_format.type is not valid: json_object is not supported by this model"
   - 不传 response_format：200 OK，模型按 prompt 输出 `{"ok": true}`
   - **结论**：豆包 Coding Plan 不是"网关静默剥离返回 200"，而是"网关 400 拒绝"——原方案 `BadRequestError` fallback 其实能捕获，但探测必须 `drop_params=True` + `allowed_openai_params` 透传才会真正触达豆包网关

2. **GLM 端点**（`config/user-config - glm.json`，model=`xopglm5`）：
   - `json_schema` strict + `drop_params=False`：200 OK，但响应 `{"oko":` （字段名漂移 + 截断 + 非法 JSON）
   - `json_object` + `drop_params=True`：200 OK，但响应 `{"ok": true}\n}` 或带额外 tab（3 次都漂移，json.loads 失败）
   - 不传 response_format：200 OK，响应 `{"ok": true}`（合法 JSON）
   - **结论**：GLM 是真正的"静默降级"——provider 接受 response_format 参数（不报 400），但模型不真正遵守 schema 约束，输出仍含额外字符导致 json.loads 失败。`BadRequestError` fallback 不触发，必须靠响应内容判定

3. **`LiteLLMSession.chat`**（`agent/generic/litellm_adapter.py:368-370`）：在传 response_format 时强制 `drop_params=True`——探测必须复用此路径，不能绕过

4. **`allowed_openai_params`** 是 LiteLLM 透传 response_format 到 provider 的关键开关

---

## 现状分析

### 1. 用户痛点

不同 LLM 厂商对 `response_format` 支持差异大：
- OpenAI：真正支持 `json_schema` strict + `json_object`
- 豆包 Coding Plan：网关 400 拒绝（不是静默剥离）
- GLM：网关接受但模型不遵守 schema，输出漂移

当前 LightRAG `_llm_model_func`（`niu_api/internal/lightrag_manager.py:178-194`）的 `BadRequestError` fallback 能捕获豆包 400，但**捕获不到 GLM 的静默漂移**（200 + 非法 JSON），依赖 `json_repair.loads()` 容错——但实体类型/字段名漂移仍会导致知识图谱质量下降。

用户在 `config/user-config.json` 的 `lightrag_llm.litellm_kwargs.allowed_openai_params: ["response_format"]` 是二值开关，丢失了"模型支持哪种格式"的信息。OpenAI 官方 `response_format.type` 有 3 个取值，约束力递减，不同厂商支持档位不同。

### 2. 现有代码链路（已确认）

| 组件 | 文件:行号 | 说明 |
|------|-----------|------|
| LightRAG LLM 调用 | `niu_api/internal/lightrag_manager.py:109-220` | `_llm_model_func` async 函数 |
| response_format 构造 | `lightrag_manager.py:132-145` | 仅 `keyword_extraction=True` 时构造（写死 json_schema） |
| BadRequestError fallback | `lightrag_manager.py:178-194` | 捕获 400 错误，prompt-only 重试（仅 keyword_extraction） |
| 配置加载 | `niu_api/llm_proxy.py:191-257` `get_llm_config(use_lightrag_config)` | `litellm_kwargs` 透传 |
| LiteLLMSession | `agent/generic/litellm_adapter.py:334-632` `chat()` | 接收 `response_format` 参数；L368-370 在传 response_format 时强制 `drop_params=True` |
| 测试连接 API | `niu_api/compat.py:1227-1319` `/api/test-llm` | **启动器复用**（见下） |
| 设置窗口前端 | `ui/main/windows/settings/index.html:380-446` `testAndSave()` | 调 `electronAPI.testConnection` → `/api/test-llm` |
| 配置 MCP 工具 | `mcp-servers/config-manager/src/niu_config_manager/__init__.py:505-583` | `get/set_lightrag_llm_config` |

### 3. `/api/test-llm` 复用情况（关键约束）

**已确认**：`/api/test-llm` 被启动器 `launcher/src/main.rs` 在两处复用：
- L1780/1790：启动后预检（body=`{}`，从文件读配置，25s 超时，失败重试 1 次）
- L1826/1840：settings 窗口打开后后台轮询（每 3 秒一次，body=`{}`）
- Rust 端 `TestLlmResult` 结构体（`main.rs:1756`）只解析 `{success, message, error}` 三字段

**约束**：
- **`/api/test-llm` 不能改动**：响应结构、耗时、失败率都会影响启动器判断 LLM 可用性
- **探测逻辑必须独立端点**：只被设置窗口 `testAndSave()` 调用，启动器不感知
- **探测端点失败不影响设置窗口保存**：探测异常时保留旧配置，不让用户卡在设置窗口

### 4. response_format 三档递进（按约束力从强到弱）

| 档位 | type | 结构 | 约束 | 真实支持情况 |
|------|------|------|------|--------------|
| **Tier 1（最强）** | `json_schema` | `{"type": "json_schema", "json_schema": {"name": ..., "strict": true, "schema": {...}}}` | 输出严格匹配 schema | OpenAI 真正支持；豆包 400 拒绝；GLM 接受但输出漂移 |
| **Tier 2（中等）** | `json_object` | `{"type": "json_object"}` | 输出合法 JSON，不约束字段 | OpenAI 支持；豆包 400 拒绝；GLM 接受但输出漂移 |
| **Tier 3（最弱）** | 无 response_format | 不传 | 纯 prompt + `json_repair.loads()` 客户端容错 | 所有厂商兜底 |

### 5. 探测结果定义（修正后）

| 结果 | 判定条件 | 写入 `response_format_mode` | 写入 `allowed_openai_params` |
|------|---------|------------------------------|------------------------------|
| `json_schema` | Tier 1 调用返回 200 + json.loads 成功 + isinstance dict + 含 "ok" 字段 | `"json_schema"` | `["response_format"]` |
| `json_object` | Tier 1 失败（异常或响应非合法 JSON）+ Tier 2 调用返回 200 + json.loads 成功 + isinstance dict | `"json_object"` | `["response_format"]` |
| `prompt_only` | Tier 1 + Tier 2 都失败（异常或响应非合法 JSON） | `"prompt_only"` | `[]` |
| `probe_failed` | 探测本身异常（超时/网络/API Key 错/不支持 provider 路由） | **不修改**（保留旧值） | **不修改** |

**判定细节**：
- Tier 1 (json_schema strict) 要求响应含 `"ok"` 字段（schema 约束字段名）
- Tier 2 (json_object) 不要求含 `"ok"` 字段（json_object 不约束字段名，只要合法 JSON dict 即可）
- 任何 tier 抛 `BadRequestError` 或 `UnsupportedParamsError` → 该 tier 失败，降级下一 tier
- 任何 tier 200 但 json.loads 失败或非 dict → 该 tier `gateway_blocked`，降级下一 tier

### 6. 配置写入策略

- **新字段**：`lightrag_llm.litellm_kwargs.response_format_mode`（值：`json_schema` / `json_object` / `prompt_only`）
- **兼容字段**：`lightrag_llm.litellm_kwargs.allowed_openai_params` 保留（前两档 `["response_format"]`，prompt_only 档 `[]`），双写保证旧逻辑兼容
- **lightrag_llm 为空时**：探测用主 `llm` 段配置（场景二/三：LightRAG 用主 Agent 同一模型），结果写入主 `llm.litellm_kwargs`
- **探测时机**：
  - 设置窗口"测试连接并保存"按钮触发（用户主动）
  - **升级后首次启动**后台自动触发（若检测到无 `response_format_mode` 键）
- **探测 token 消耗**：每档 `max_tokens=50`，最坏情况（测到 Tier 2）约 200 token

### 7. 风险与边界

1. **不破坏 BadRequestError fallback**：`lightrag_manager.py:186-194` 的 fallback 保留——探测结果只决定**构造哪种 response_format**，构造后调用失败（如偶发 400）依然走 fallback
2. **探测复用 `LiteLLMSession.chat`**：不再绕过自构造 `litellm.completion` kwargs，确保探测和运行时 kwargs 完全一致（含 `drop_params=True`、`stream=True`、`temperature` 等）
3. **`allowed_openai_params` 探测时必须含 `["response_format"]`**：让 LiteLLM 透传 response_format 到 provider，否则 volcengine router 在客户端拒绝（请求未发出）
4. **配置文件版本字段**：不引入 `response_format_probe` 历史记录字段（YAGNI），探测结果直接覆盖 `response_format_mode`
5. **MCP 工具扩展**：`get_lightrag_llm_config` 返回值增加 `litellm_kwargs` 字段（含 `response_format_mode` 和 `allowed_openai_params`），方便主 Agent 通过 MCP 查询当前真实状态
6. **`set_lightrag_llm_config` 不扩展参数**：用户通过设置窗口修改 `litellm_kwargs`，MCP 工具仍只控制顶层字段
7. **启动器感知**：`/api/test-llm` 完全不动，启动器无任何代码改动
8. **升级后引导探测**：检测 `lightrag_llm.litellm_kwargs` 无 `response_format_mode` 键时后台触发探测，不阻塞启动（在 niu_api 启动后的后台线程跑）

---

## 文件结构

### 新建文件
- `tests/test_response_format_probe.py` — 探测逻辑单元测试 + 集成测试
- `docs/superpowers/plans/2026-07-19-response-format-real-env-verification.md` — 真实环境验证报告（已写）

### 修改文件
- `niu_api/internal/lightrag_manager.py`（L132-145）— 根据 `response_format_mode` 决定构造哪种 response_format
- `niu_api/compat.py`（L1319 之后）— 新增 `/api/probe-response-format` 端点 + 3 个辅助函数
- `niu_api/internal/lightrag_manager.py`（启动钩子）— 新增升级后首次启动后台探测
- `ui/main/windows/settings/index.html`（L380-446 `testAndSave()`）— 测试通过后追加探测调用，根据结果修改 litellm_kwargs
- `ui/main/preload-settings.js` — 暴露 `probeResponseFormat` IPC
- `ui/main/main.js` — 注册 `probe-response-format` IPC handler
- `mcp-servers/config-manager/src/niu_config_manager/__init__.py`（L505-521 `get_lightrag_llm_config`）— 返回值增加 `litellm_kwargs` 字段
- `docs/manual-user-guide.md`（1.2 节）— 补充"格式化输出能力自动探测"说明

### 不修改
- `niu_api/compat.py:1227-1319` `/api/test-llm`（启动器复用，禁止改动）
- `agent/generic/litellm_adapter.py`（`drop_params` 逻辑已正确，不动）
- `niu_api/llm_proxy.py`（配置加载已正确，不动）
- `launcher/src/main.rs`（启动器逻辑不动）
- `mcp-servers/config-manager/.../__init__.py:524` `set_lightrag_llm_config`（不扩展参数）

---

## Task 1: 后端决策函数 `_resolve_response_format`（纯函数易测）

**Files:**
- Create: `tests/test_response_format_probe.py`
- Modify: `niu_api/internal/lightrag_manager.py:92`（新增函数）+ `L132-145`（接入决策）

### Step 1.1：写失败测试 — `_resolve_response_format` 决策函数

- [ ] **Step 1：写失败测试**

`tests/test_response_format_probe.py`：
```python
"""response_format 决策函数单元测试。

验证 _resolve_response_format 根据 litellm_kwargs.response_format_mode
决定构造哪种 response_format。这是纯函数，不调 LLM，仅检查配置。
"""
import pytest
from niu_api.internal.lightrag_manager import _resolve_response_format


def test_json_schema_mode_returns_json_schema_response_format():
    """response_format_mode=json_schema → 返回 json_schema strict 结构"""
    config = {"litellm_kwargs": {"response_format_mode": "json_schema"}}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "keyword_extraction"


def test_json_object_mode_returns_json_object_response_format():
    """response_format_mode=json_object → 返回 {"type": "json_object"}"""
    config = {"litellm_kwargs": {"response_format_mode": "json_object"}}
    rf = _resolve_response_format(config)
    assert rf == {"type": "json_object"}


def test_prompt_only_mode_returns_none():
    """response_format_mode=prompt_only → 返回 None（不构造）"""
    config = {"litellm_kwargs": {"response_format_mode": "prompt_only"}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_missing_mode_returns_none():
    """litellm_kwargs 无 response_format_mode 键 → 返回 None（保守降级）"""
    config = {"litellm_kwargs": {}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_missing_litellm_kwargs_returns_none():
    """配置无 litellm_kwargs 键 → 返回 None"""
    config = {}
    rf = _resolve_response_format(config)
    assert rf is None


def test_unknown_mode_returns_none():
    """response_format_mode 是未知值 → 返回 None（保守降级）"""
    config = {"litellm_kwargs": {"response_format_mode": "unknown_mode"}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_legacy_allowed_openai_params_still_supported():
    """旧配置只有 allowed_openai_params=["response_format"]（无 response_format_mode）
    → 兼容旧配置，返回 json_schema（默认最强档）

    Why: 旧版本用户配置文件没有 response_format_mode 字段，本次升级不应破坏。
    升级后首次启动后台探测会自动写入 response_format_mode。
    """
    config = {"litellm_kwargs": {"allowed_openai_params": ["response_format"]}}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"


def test_legacy_empty_allowed_openai_params_returns_none():
    """旧配置 allowed_openai_params=[]（无 response_format_mode）→ 返回 None"""
    config = {"litellm_kwargs": {"allowed_openai_params": []}}
    rf = _resolve_response_format(config)
    assert rf is None


def test_json_schema_mode_with_thinking_kwargs():
    """豆包配置：response_format_mode=json_schema + thinking={type:disabled} 共存

    验证 thinking 参数不影响 response_format 决策。
    """
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    rf = _resolve_response_format(config)
    assert rf is not None
    assert rf["type"] == "json_schema"


def test_resolve_does_not_modify_config_no_side_effects():
    """_resolve_response_format 不修改 config（无副作用）

    Why: v4 曾用 pop 副作用修改 config，导致 keyword_extraction=True 与 False
    两种调用模式的 config_key 不一致，破坏 _get_litellm_session 缓存。
    v5 改为 get（无副作用），response_format_mode 字段在 _llm_model_func 内
    通过 _strip_response_format_mode 单独剔除。
    """
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    original = {"litellm_kwargs": dict(config["litellm_kwargs"])}
    _resolve_response_format(config)
    # config 不应被修改
    assert config["litellm_kwargs"] == original["litellm_kwargs"]
    assert "response_format_mode" in config["litellm_kwargs"]


def test_strip_response_format_mode_returns_new_dict_without_mode():
    """_strip_response_format_mode 返回新 dict，剔除 response_format_mode"""
    from niu_api.internal.lightrag_manager import _strip_response_format_mode
    config = {"litellm_kwargs": {
        "response_format_mode": "json_schema",
        "thinking": {"type": "disabled"},
        "allowed_openai_params": ["response_format"],
    }}
    stripped = _strip_response_format_mode(config)
    # 原config不修改
    assert "response_format_mode" in config["litellm_kwargs"]
    # 新config剔除 response_format_mode
    assert "response_format_mode" not in stripped["litellm_kwargs"]
    # 其他字段保留
    assert stripped["litellm_kwargs"]["thinking"] == {"type": "disabled"}
    assert stripped["litellm_kwargs"]["allowed_openai_params"] == ["response_format"]


def test_strip_response_format_mode_returns_same_dict_when_no_mode():
    """无 response_format_mode 键时，_strip 返回原 config（不复制）"""
    from niu_api.internal.lightrag_manager import _strip_response_format_mode
    config = {"litellm_kwargs": {"thinking": {"type": "disabled"}}}
    stripped = _strip_response_format_mode(config)
    # 无需复制，返回原对象
    assert stripped is config


def test_strip_response_format_mode_handles_missing_litellm_kwargs():
    """config 无 litellm_kwargs 键时，_strip 返回原 config"""
    from niu_api.internal.lightrag_manager import _strip_response_format_mode
    config = {}
    stripped = _strip_response_format_mode(config)
    assert stripped is config
```

- [ ] **Step 2：跑测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./python/bin/python -m pytest tests/test_response_format_probe.py -v
```
Expected: 9 个测试全 FAIL（`ImportError: cannot import name '_resolve_response_format'`）

### Step 1.2：实现 `_resolve_response_format`

- [ ] **Step 3：实现决策函数**

在 `niu_api/internal/lightrag_manager.py` 的 `_build_llm_model_func` 函数定义**之前**（约 L92 之前），新增：

```python
def _resolve_response_format(config: dict) -> Optional[dict]:
    """根据 litellm_kwargs.response_format_mode 决定构造哪种 response_format。

    返回值：
    - {"type": "json_schema", "json_schema": {...strict...}}: 最强档，schema 严格匹配
    - {"type": "json_object"}: 中等档，仅约束合法 JSON
    - None: 最弱档，prompt-only + json_repair 客户端容错

    本函数无副作用，不修改 config。response_format_mode 字段在 _llm_model_func
    内通过 _strip_response_format_mode 单独剔除，避免透传给 LiteLLM provider
    （response_format_mode 是项目自定义字段，不是 OpenAI 标准也不是 LiteLLM
    认识的字段）。

    配置优先级：
    1. response_format_mode 字段（探测结果，权威）
    2. allowed_openai_params 含 "response_format"（旧版本兼容，默认 json_schema 档）
    3. 都没有 → None（保守降级，未探测过）

    Why: OpenAI response_format.type 有 3 档（json_schema/json_object/无），
    不同厂商支持档位不同。探测端点按 json_schema → json_object → prompt_only
    递进测试，结果写入 response_format_mode。本函数运行时读出来决定构造哪种。

    真实环境验证（2026-07-19）：
    - 豆包 Coding Plan：网关 400 拒绝 response_format → 探测后 mode=prompt_only
    - GLM：网关接受但模型输出漂移 → 探测后 mode=prompt_only
    - OpenAI：真正支持 → 探测后 mode=json_schema
    """
    litellm_kwargs = config.get("litellm_kwargs") or {}
    mode = litellm_kwargs.get("response_format_mode")
    if mode == "json_schema":
        from lightrag.types import GPTKeywordExtractionFormat
        schema = GPTKeywordExtractionFormat.model_json_schema()
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "keyword_extraction",
                "strict": True,
                "schema": schema,
            },
        }
    if mode == "json_object":
        return {"type": "json_object"}
    if mode == "prompt_only":
        return None
    # 旧版本兼容：无 response_format_mode 但有 allowed_openai_params
    allowed = litellm_kwargs.get("allowed_openai_params") or []
    if "response_format" in allowed:
        from lightrag.types import GPTKeywordExtractionFormat
        schema = GPTKeywordExtractionFormat.model_json_schema()
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "keyword_extraction",
                "strict": True,
                "schema": schema,
            },
        }
    return None


def _strip_response_format_mode(config: dict) -> dict:
    """剔除 config["litellm_kwargs"]["response_format_mode"] 字段，返回新 dict。

    Why: response_format_mode 是项目自定义字段，不是 OpenAI 标准也不是
    LiteLLM 认识的字段。如果留在 litellm_kwargs：
    1. 会被 LiteLLMSession.chat 通过 request_params.update(self.litellm_kwargs)
       (litellm_adapter.py:377-378) 透传给 litellm.completion，可能触发 provider
       400 拒绝未知参数
    2. 会进入 _get_litellm_session 的 config_key 计算（lightrag_manager.py:66
       tuple(sorted(config.get("litellm_kwargs", {}).items()))），影响缓存键

    本函数返回新 dict 不修改原 config（无副作用），调用方用返回值传给
    _get_litellm_session。原 config 仍含 response_format_mode，下次 _resolve_response_format
    调用仍能读到，但不再透传给 provider 也不参与 config_key。

    Why 不直接 pop：v4 曾用 pop 副作用修改 config，导致 keyword_extraction=True
    与 False 两种调用模式的 config_key 不一致，破坏 _get_litellm_session 缓存。
    本函数返回新 dict 避免此问题。
    """
    litellm_kwargs = config.get("litellm_kwargs") or {}
    if "response_format_mode" not in litellm_kwargs:
        return config  # 无需复制
    new_litellm_kwargs = {k: v for k, v in litellm_kwargs.items() if k != "response_format_mode"}
    return {**config, "litellm_kwargs": new_litellm_kwargs}
```

- [ ] **Step 4：跑测试确认通过**

```bash
./python/bin/python -m pytest tests/test_response_format_probe.py -v
```
Expected: 9 个测试全 PASS

### Step 1.3：在 `_llm_model_func` 接入决策函数

- [ ] **Step 5：修改 `_llm_model_func` L126-145**

将 `niu_api/internal/lightrag_manager.py:126-145` 的：
```python
        # 3. Handle keyword_extraction: try response_format, fallback to prompt
        # Models that support json_schema Structured Outputs (e.g. OpenAI) get the
        # reliable response_format path. Models that don't (e.g. ark-code-latest)
        # raise BadRequestError — we catch that, append JSON instructions to prompt,
        # and retry without response_format. LightRAG's json_repair.loads() handles
        # parsing the text-only output.
        response_format = None
        kw_prompt_suffix = ""
        if keyword_extraction:
            from lightrag.types import GPTKeywordExtractionFormat
            schema = GPTKeywordExtractionFormat.model_json_schema()
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "keyword_extraction",
                    "strict": True,
                    "schema": schema,
                },
            }
            kw_prompt_suffix = '\n\nReturn your response as a JSON object with "high_level_keywords" and "low_level_keywords" arrays.'
```

改为：
```python
        # 3. Handle keyword_extraction: 根据探测结果构造对应 response_format
        # 探测由设置窗口"测试连接并保存"触发，按 json_schema → json_object → prompt_only
        # 递进测试，结果写入 lightrag_llm.litellm_kwargs.response_format_mode。
        # 真实环境验证（2026-07-19）：
        # - 豆包 Coding Plan：网关 400 拒绝，探测后 mode=prompt_only
        # - GLM：网关接受但模型输出漂移，探测后 mode=prompt_only
        # BadRequestError fallback 保留兜底（偶发 400 时仍走 prompt-only 重试）。
        response_format = None
        kw_prompt_suffix = ""
        if keyword_extraction:
            config = get_llm_config(use_lightrag_config=True)
            response_format = _resolve_response_format(config)
            kw_prompt_suffix = '\n\nReturn your response as a JSON object with "high_level_keywords" and "low_level_keywords" arrays.'
        else:
            config = get_llm_config(use_lightrag_config=True)
```

**注意**：
- 原 L158 `config = get_llm_config(use_lightrag_config=True)` 被提前到 L132 之前。删除原 L158 那行（避免重复调用，且变量已被使用）。
- `keyword_extraction=True` 和 `False` 两个分支都调 `get_llm_config`，确保 `config` 变量在两种模式下都有值（避免下游 `sync_call` 内 `_get_litellm_session(config)` 报 NameError）。
- **关键：`sync_call` 内调 `_get_litellm_session(config)` 前必须先 strip**，避免 `response_format_mode` 字段进入 config_key（破坏缓存键一致性）和透传给 provider（可能触发 400）。修改 `sync_call` 内 L180 `session = _get_litellm_session(config)` 为：
  ```python
  session = _get_litellm_session(_strip_response_format_mode(config))
  ```
  `_strip_response_format_mode` 返回新 dict 不修改原 config（无副作用），原 config 仍含 `response_format_mode` 供下次 `_resolve_response_format` 读取，但不再参与 config_key 也不透传 provider。

- [ ] **Step 6：语法检查 + 跑 lightrag_manager 相关测试确认无回归**

```bash
./python/bin/python -c "from niu_api.internal.lightrag_manager import _resolve_response_format; print('OK')"
./python/bin/python -m pytest tests/test_lightrag_manager.py -v
```
Expected: 语法 OK + 现有测试全 PASS。

特别关注 `test_keyword_extraction_builds_response_format`（`tests/test_lightrag_manager.py:255`）：
- 该测试用 mock config 不含 `response_format_mode` 也不含 `allowed_openai_params`
- 新逻辑会让 `response_format=None`，现有断言 `captured_response_format is not None` 会 FAIL
- **必须同步修改 fixture**：给 mock config 加 `"litellm_kwargs": {"response_format_mode": "json_schema"}` 或 `"allowed_openai_params": ["response_format"]`

具体修改：找到 `test_keyword_extraction_builds_response_format` 测试函数，在 mock config 字典里追加 `"litellm_kwargs": {"response_format_mode": "json_schema"}` 字段。如果该测试通过 `mock.patch` 替换 `get_llm_config`，确保返回的 config 含该字段。

- [ ] **Step 7：提交**

```bash
git add tests/test_response_format_probe.py tests/test_lightrag_manager.py niu_api/internal/lightrag_manager.py
git commit -m "feat(lightrag): _resolve_response_format 按 response_format_mode 决定构造档位

OpenAI response_format.type 有 3 档（json_schema/json_object/无），不同厂商
支持档位不同。引入 response_format_mode 配置字段，运行时根据探测结果决定构造
哪种格式。

真实环境验证发现：
- 豆包 Coding Plan：网关 400 拒绝（非静默剥离）
- GLM：网关接受但模型输出漂移
两者探测后均降级 prompt_only。

兼容旧配置：无 response_format_mode 时按 allowed_openai_params 退化。"
```

---

## Task 2: 后端探测端点 `/api/probe-response-format`（3 档递进，复用 LiteLLMSession）

**Files:**
- Modify: `niu_api/compat.py:1319` 之后追加新端点 + 3 个辅助函数
- Test: `tests/test_response_format_probe.py` 追加测试

### Step 2.1：写失败测试 — 探测辅助函数

- [ ] **Step 1：追加测试到 `tests/test_response_format_probe.py`**

```python
"""探测辅助函数单元测试。

_classify_probe_response 根据响应文本判定 supported/gateway_blocked。
_build_probe_messages 构造探测 prompt。
_build_probe_response_format_json_schema / json_object 构造探测 response_format。
纯函数，不调真实 LLM。
"""
import pytest
from niu_api.compat import (
    _classify_probe_response_tier1,
    _classify_probe_response_tier2,
    _build_probe_messages,
    _build_probe_response_format_json_schema,
    _build_probe_response_format_json_object,
)


def test_build_probe_response_format_json_schema_structure():
    """json_schema 档构造 OpenAI Structured Outputs 标准结构"""
    rf = _build_probe_response_format_json_schema()
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "probe_response_format"
    assert rf["json_schema"]["strict"] is True
    assert "ok" in rf["json_schema"]["schema"]["properties"]


def test_build_probe_response_format_json_object_structure():
    """json_object 档构造 {"type": "json_object"}"""
    rf = _build_probe_response_format_json_object()
    assert rf == {"type": "json_object"}


def test_build_probe_messages_returns_single_user_message_with_json_instruction():
    """探测消息含 JSON 字样（OpenAI json_object 模式硬性要求 prompt 含 'json'）"""
    msgs = _build_probe_messages()
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert "json" in msgs[0]["content"].lower()
    assert "ok" in msgs[0]["content"]


# Tier 1 (json_schema strict) 要求响应是合法 JSON dict + 含 ok 字段
def test_classify_tier1_supported_when_valid_json_with_ok_field():
    """响应是 {"ok": true} → supported"""
    assert _classify_probe_response_tier1('{"ok": true}') == "supported"


def test_classify_tier1_supported_when_json_with_extra_fields():
    """响应是 {"ok": true, "extra": "ignored"} → supported（schema strict 容忍额外字段）"""
    assert _classify_probe_response_tier1('{"ok": true, "extra": "ignored"}') == "supported"


def test_classify_tier1_gateway_blocked_when_plain_text():
    """响应是纯文本（如 GLM json_schema 实测输出 {"oko":）→ gateway_blocked"""
    assert _classify_probe_response_tier1('I am doing fine.') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_truncated_json():
    """响应是截断的非合法 JSON（如 GLM json_schema 实测 {"oko":）→ gateway_blocked"""
    assert _classify_probe_response_tier1('{"oko":') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_empty():
    """响应空 → gateway_blocked"""
    assert _classify_probe_response_tier1('') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_markdown_wrapped():
    """响应是 ```json ...``` 包裹 → gateway_blocked（非纯 JSON）"""
    assert _classify_probe_response_tier1('```json\n{"ok": true}\n```') == "gateway_blocked"


def test_classify_tier1_gateway_blocked_when_json_without_ok_field():
    """响应是 {"foo": "bar"}（合法 JSON 但无 ok 字段）→ gateway_blocked
    （json_schema strict 要求字段匹配，无 ok 说明 schema 未生效）"""
    assert _classify_probe_response_tier1('{"foo": "bar"}') == "gateway_blocked"


# Tier 2 (json_object) 不要求含 ok 字段，只要求合法 JSON dict
def test_classify_tier2_supported_when_valid_json_dict():
    """响应是 {"foo": "bar"}（合法 JSON dict，无 ok）→ supported
    json_object 不约束字段名，只要合法 JSON dict 即可"""
    assert _classify_probe_response_tier2('{"foo": "bar"}') == "supported"


def test_classify_tier2_supported_when_json_with_ok_field():
    """响应是 {"ok": true} → supported"""
    assert _classify_probe_response_tier2('{"ok": true}') == "supported"


def test_classify_tier2_gateway_blocked_when_plain_text():
    """响应是纯文本 → gateway_blocked"""
    assert _classify_probe_response_tier2('I am doing fine.') == "gateway_blocked"


def test_classify_tier2_gateway_blocked_when_truncated_json():
    """响应是 {"ok": true}\\n} （GLM json_object 实测含额外字符）→ gateway_blocked"""
    assert _classify_probe_response_tier2('{"ok": true}\n}') == "gateway_blocked"


def test_classify_tier2_gateway_blocked_when_empty():
    """响应空 → gateway_blocked"""
    assert _classify_probe_response_tier2('') == "gateway_blocked"
```

- [ ] **Step 2：跑测试确认失败**

```bash
./python/bin/python -m pytest tests/test_response_format_probe.py::test_build_probe_response_format_json_schema_structure -v
```
Expected: FAIL（`ImportError: cannot import name '_classify_probe_response_tier1'`）

### Step 2.2：实现探测辅助函数

- [ ] **Step 3：在 `niu_api/compat.py` 的 `/api/test-llm` 端点之后（约 L1320）新增辅助函数**

```python
def _build_probe_response_format_json_schema() -> dict:
    """构造 Tier 1 探测用 response_format：json_schema strict。

    要求模型返回 {"ok": true}，schema 严格匹配。选最小 schema 降低 token 消耗。
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "probe_response_format",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "ok": {"type": "boolean"}
                },
                "required": ["ok"],
                "additionalProperties": False,
            },
        },
    }


def _build_probe_response_format_json_object() -> dict:
    """构造 Tier 2 探测用 response_format：json_object。

    仅约束输出合法 JSON，不约束字段。
    """
    return {"type": "json_object"}


def _build_probe_messages() -> list[dict]:
    """构造探测消息。prompt 含 'json' 字样（OpenAI json_object 模式硬性要求），
    且显式要求 {"ok": true}，即使 response_format 被网关剥离也能通过响应格式判定。"""
    return [{
        "role": "user",
        "content": 'Respond with a JSON object: {"ok": true}. Do not include any other text.',
    }]


def _classify_probe_response_tier1(text: str) -> str:
    """Tier 1 (json_schema strict) 判定。要求响应是合法 JSON dict 且含 "ok" 字段。

    返回值：
    - "supported": 响应合法 JSON dict + 含 "ok" 字段
    - "gateway_blocked": 响应非合法 JSON 或无 "ok" 字段（schema 未生效）

    真实环境验证（2026-07-19）：GLM json_schema 实测返回 {"oko":（字段名漂移 + 截断），
    走 gateway_blocked 分支降级到 Tier 2。
    """
    import json
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return "gateway_blocked"
    if not isinstance(data, dict) or "ok" not in data:
        return "gateway_blocked"
    return "supported"


def _classify_probe_response_tier2(text: str) -> str:
    """Tier 2 (json_object) 判定。只要求响应是合法 JSON dict，不要求字段名。

    返回值：
    - "supported": 响应合法 JSON dict
    - "gateway_blocked": 响应非合法 JSON 或非 dict

    Why 不要求含 ok 字段：json_object 仅约束输出合法 JSON，不约束字段名。
    模型可能返回 {"result": true} 或 {"status": "ok"}，都算 supported。

    真实环境验证（2026-07-19）：GLM json_object 实测返回 {"ok": true}\\n}（含额外字符），
    json.loads 失败走 gateway_blocked 分支降级到 Tier 3。
    """
    import json
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return "gateway_blocked"
    if not isinstance(data, dict):
        return "gateway_blocked"
    return "supported"
```

- [ ] **Step 4：跑测试确认通过**

```bash
./python/bin/python -m pytest tests/test_response_format_probe.py -v
```
Expected: 全部 PASS

### Step 2.3：实现 `/api/probe-response-format` 端点（3 档递进，复用 LiteLLMSession）

- [ ] **Step 5：在 `niu_api/compat.py` 辅助函数之后追加新端点**

```python
@router.post("/api/probe-response-format")
async def probe_response_format(request: Request) -> dict:
    """递进探测当前 LLM 配置对 response_format 的支持档位。

    3 档递进（最强→最弱）：
    - Tier 1: json_schema strict → 200 + 合法 JSON dict + 含 ok → json_schema
    - Tier 2: json_object → 200 + 合法 JSON dict → json_object
    - Tier 3: 都失败 → prompt_only

    每档失败条件：
    - BadRequestError/UnsupportedParamsError（模型/网关 4xx 拒绝）
    - 200 + 非合法 JSON（网关接受但模型输出漂移，如 GLM）

    注意：BadRequestError 抛出时机取决于 provider 行为：
    - 同步抛（litellm.completion 调用立即返回 4xx，如豆包网关拒绝）→
      `_try_tier` 的 except 捕获 → 返回 "model_rejected"
    - 流式中途抛（stream chunk 阶段返回错误）→ LiteLLMSession.chat 内部
      `except Exception`（litellm_adapter.py:531-585）吞掉返回 MockResponse →
      `_try_tier` 收不到异常，text 为空或部分 → 判 "gateway_blocked"
    两种路径最终 mode 判定均为降级下一 tier，不影响 mode 结果，仅影响 reason 字段措辞。

    探测本身异常（超时/网络/API Key 错）→ probe_failed，不修改配置。

    真实环境验证（2026-07-19）：
    - 豆包 Coding Plan：Tier 1/2 网关 400 拒绝 → prompt_only
    - GLM：Tier 1/2 网关 200 但输出漂移 → prompt_only
    - OpenAI：Tier 1 真正支持 → json_schema

    约束：本端点独立于 /api/test-llm（启动器复用，禁止改动响应结构）。
    """
    from agent.generic.litellm_adapter import LiteLLMSession
    from litellm import BadRequestError, UnsupportedParamsError

    try:
        body = await request.json()
    except Exception:
        body = {}
    body = {k.lower(): v for k, v in body.items()} if body else {}

    if body and body.get("apikey"):
        config = body
    else:
        from niu_api.llm_proxy import get_llm_config
        try:
            config = get_llm_config(use_lightrag_config=True)
        except Exception as e:
            return {"result": "probe_failed", "reason": f"读取配置失败: {e}", "mode": None, "raw_response": ""}
    config = {k.lower(): v for k, v in config.items()}

    if not config.get("apikey"):
        return {"result": "probe_failed", "reason": "API Key 未配置", "mode": None, "raw_response": ""}
    if not config.get("apibase"):
        return {"result": "probe_failed", "reason": "API 地址未配置", "mode": None, "raw_response": ""}
    if not config.get("model"):
        return {"result": "probe_failed", "reason": "模型名称未配置", "mode": None, "raw_response": ""}

    # 探测用 LiteLLMSession：复用运行时调用路径（含 drop_params=True 自动设置、
    # stream=True、temperature、provider_params 等），确保探测和运行时行为一致。
    # 关键：litellm_kwargs 必须含 allowed_openai_params=["response_format"]，
    # 否则 LiteLLM volcengine router 在客户端拒绝抛 UnsupportedParamsError，
    # 请求不会真正发到 provider 网关。
    probe_litellm_kwargs = {**config.get("litellm_kwargs", {})}
    probe_litellm_kwargs["allowed_openai_params"] = ["response_format"]
    probe_litellm_kwargs["max_tokens"] = 50

    base_llm_config = {
        "api_type": config.get("type", "openai"),
        "apikey": config["apikey"],
        "apibase": config["apibase"],
        "model": config["model"],
        "reasoning_effort": None,
        "provider": config.get("provider", ""),
        # temperature 与运行时 _get_litellm_session 一致（默认 0.2），
        # 避免探测和运行时采样随机性差异
        "temperature": config.get("temperature", 0.2),
        "litellm_kwargs": probe_litellm_kwargs,
        "read_timeout": 15,
    }

    messages = _build_probe_messages()

    def _try_tier(response_format: Optional[dict]) -> tuple[str, str]:
        """单档探测。返回 (tier_result, raw_text)。

        tier_result:
        - "supported": 200 + 合法 JSON（Tier 1 还要求含 ok 字段）
        - "gateway_blocked": 200 + 非合法 JSON
        - "model_rejected": BadRequestError/UnsupportedParamsError（4xx 拒绝）
        - "probe_error": 其他异常
        """
        try:
            session = LiteLLMSession(cfg=base_llm_config)
            gen = session.chat(messages=messages, response_format=response_format)
            chunks = []
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration:
                pass
            text = "".join(chunks)
            if response_format is not None and response_format.get("type") == "json_schema":
                tier = _classify_probe_response_tier1(text)
            elif response_format is not None and response_format.get("type") == "json_object":
                tier = _classify_probe_response_tier2(text)
            else:
                # 无 response_format（不应进入此分支，探测必有 response_format）
                tier = "gateway_blocked"
            return tier, text
        except (BadRequestError, UnsupportedParamsError) as e:
            return "model_rejected", str(e)[:200]
        except Exception as e:
            return "probe_error", str(e)[:200]

    # Tier 1: json_schema strict
    try:
        tier1_result, tier1_text = await asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_schema()),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return {"result": "probe_failed", "reason": "Tier 1 探测超时（30s）", "mode": None, "raw_response": ""}

    if tier1_result == "supported":
        return {
            "result": "supported",
            "mode": "json_schema",
            "reason": "Tier 1 通过：模型+网关均支持 json_schema strict 模式",
            "raw_response": tier1_text[:200],
        }
    if tier1_result == "probe_error":
        return {"result": "probe_failed", "reason": f"Tier 1 异常: {tier1_text}", "mode": None, "raw_response": ""}

    # Tier 2: json_object
    try:
        tier2_result, tier2_text = await asyncio.wait_for(
            asyncio.to_thread(_try_tier, _build_probe_response_format_json_object()),
            timeout=30,
        )
    except asyncio.TimeoutError:
        return {"result": "probe_failed", "reason": "Tier 2 探测超时（30s）", "mode": None, "raw_response": ""}

    if tier2_result == "supported":
        return {
            "result": "supported",
            "mode": "json_object",
            "reason": f"Tier 1 失败（{tier1_result}），Tier 2 通过：模型支持 json_object 模式",
            "raw_response": tier2_text[:200],
        }
    if tier2_result == "probe_error":
        return {"result": "probe_failed", "reason": f"Tier 2 异常: {tier2_text}", "mode": None, "raw_response": ""}

    # Tier 3: 都失败，prompt_only 保底
    return {
        "result": "supported",
        "mode": "prompt_only",
        "reason": f"Tier 1（{tier1_result}）+ Tier 2（{tier2_result}）均失败，降级到 prompt-only 模式",
        "raw_response": "",
    }
```

**注意**：
- Tier 3 也返回 `"result": "supported"` 表示探测成功完成（找到了合适的档位是 prompt_only），前端据此正常保存。仅当探测本身异常才返回 `"probe_failed"`。
- `LiteLLMSession.chat` 在传 response_format 时强制 `drop_params=True`（`litellm_adapter.py:368-370`），但 `litellm_kwargs.allowed_openai_params=["response_format"]` 会让 LiteLLM 真正透传 response_format 到 provider，不会静默丢弃。

- [ ] **Step 6：语法检查**

```bash
./python/bin/python -c "from niu_api.compat import probe_response_format, _classify_probe_response_tier1, _classify_probe_response_tier2, _build_probe_response_format_json_schema, _build_probe_response_format_json_object, _build_probe_messages; print('OK')"
```
Expected: 输出 `OK`

- [ ] **Step 7：提交**

```bash
git add niu_api/compat.py tests/test_response_format_probe.py
git commit -m "feat(api): 新增 /api/probe-response-format 3 档递进探测端点（v2）

按 json_schema strict → json_object → prompt_only 递进测试，找出当前 LLM
配置支持的最强档位。每档失败条件：
- model_rejected: BadRequestError/UnsupportedParamsError（4xx）
- gateway_blocked: 200 + 非合法 JSON（网关接受但模型输出漂移，如 GLM）

探测复用 LiteLLMSession.chat 运行时路径，drop_params 和 stream 行为与
运行时完全一致。litellm_kwargs.allowed_openai_params=['response_format']
让 LiteLLM 真正透传 response_format 到 provider 网关。

独立于 /api/test-llm（启动器复用，响应结构禁止改动），仅被设置窗口调用。"
```

---

## Task 2.5: 升级后首次启动后台探测

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py`（启动钩子，在 LightRAG 初始化后追加）
- Test: `tests/test_response_format_probe.py` 追加单元测试

### Step 2.5.1：写失败测试 — `_should_auto_probe_after_upgrade` 决策函数

- [ ] **Step 1：追加测试到 `tests/test_response_format_probe.py`**

```python
"""升级后自动探测决策函数测试。

_should_auto_probe_after_upgrade 判断 lightrag_llm.litellm_kwargs 是否需要自动探测：
- 无 response_format_mode 键 → True（旧版本配置，需探测）
- 有 response_format_mode 键 → False（已探测过）
"""
import pytest
from niu_api.internal.lightrag_manager import _should_auto_probe_after_upgrade


def test_returns_true_when_no_response_format_mode():
    """lightrag_llm.litellm_kwargs 无 response_format_mode 键 → True（旧版本）"""
    config = {"lightrag_llm": {"litellm_kwargs": {"thinking": {"type": "disabled"}}}}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_true_when_no_litellm_kwargs():
    """lightrag_llm 无 litellm_kwargs 键 → True"""
    config = {"lightrag_llm": {"reasoning_effort": "none"}}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_true_when_no_lightrag_llm():
    """配置无 lightrag_llm 段 → True"""
    config = {}
    assert _should_auto_probe_after_upgrade(config) is True


def test_returns_false_when_response_format_mode_exists():
    """lightrag_llm.litellm_kwargs 含 response_format_mode 键 → False（已探测过）"""
    config = {"lightrag_llm": {"litellm_kwargs": {"response_format_mode": "prompt_only"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_false_when_response_format_mode_is_any_value():
    """response_format_mode 是任意值（含 prompt_only）都算已探测过 → False"""
    config = {"lightrag_llm": {"litellm_kwargs": {"response_format_mode": "json_schema"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_false_when_llm_has_response_format_mode():
    """llm.litellm_kwargs 含 response_format_mode（lightrag_llm 为空场景）→ False

    场景二/三：LightRAG 用主 Agent 同一模型，response_format_mode 写在 llm 段。
    """
    config = {"llm": {"litellm_kwargs": {"response_format_mode": "prompt_only"}}}
    assert _should_auto_probe_after_upgrade(config) is False


def test_returns_true_when_only_llm_has_litellm_kwargs_without_mode():
    """llm.litellm_kwargs 有内容但无 response_format_mode 键 → True（需探测）"""
    config = {"llm": {"litellm_kwargs": {"thinking": {"type": "enabled"}}}}
    assert _should_auto_probe_after_upgrade(config) is True
```

- [ ] **Step 2：跑测试确认失败**

```bash
./python/bin/python -m pytest tests/test_response_format_probe.py::test_returns_true_when_no_response_format_mode -v
```
Expected: FAIL（`ImportError: cannot import name '_should_auto_probe_after_upgrade'`）

### Step 2.5.2：实现决策函数 + 启动钩子

- [ ] **Step 3：在 `niu_api/internal/lightrag_manager.py` 的 `_resolve_response_format` 之后新增**

```python
def _should_auto_probe_after_upgrade(user_config: dict) -> bool:
    """判断是否需要在启动时自动触发 response_format 探测。

    返回 True 的条件：lightrag_llm.litellm_kwargs 和 llm.litellm_kwargs
    都无 response_format_mode 键（表示用户从旧版本升级，未探测过）。

    Why: 旧版本用户配置无 response_format_mode 字段。如果不自动探测，
    GLM 等需要探测的配置会永远走 prompt_only（_resolve_response_format
    返回 None），与"GLM 支持其他返回格式"事实矛盾。

    同时检查 llm.litellm_kwargs 是因为 lightrag_llm.model 为空时
    get_llm_config 走 fallback 用 llm 段，response_format_mode 可能
    写在 llm 段（场景二/三：LightRAG 用主 Agent 同一模型）。
    """
    lightrag_llm = user_config.get("lightrag_llm") or {}
    lightrag_kwargs = lightrag_llm.get("litellm_kwargs") or {}
    llm = user_config.get("llm") or {}
    llm_kwargs = llm.get("litellm_kwargs") or {}
    return "response_format_mode" not in lightrag_kwargs and "response_format_mode" not in llm_kwargs


def _trigger_background_probe_if_needed() -> None:
    """启动后后台探测 response_format 档位（如检测到旧版本配置）。

    在独立 daemon 线程跑，不阻塞启动流程。探测结果写入
    lightrag_llm.litellm_kwargs.response_format_mode + allowed_openai_params。

    时序说明：本函数在 LightRAG eager init 之后调用，但此时 niu_api lifespan
    可能还没 yield（FastAPI 在 yield 前不处理 HTTP 请求）。daemon 线程内先
    sleep 10s 等服务起来，然后最多重试 3 次（每次间隔 10s）。

    已开始执行的 keyword_extraction 调用会继续用旧 session（基于旧 config_key），
    下次调用 _get_litellm_session 看到 config_key 变化会自动重建 session，
    读到新配置——无时序问题。
    """
    import json
    import threading
    from pathlib import Path
    from niu_api.llm_proxy import get_llm_config

    def _probe_in_background():
        try:
            user_config_path = Path.home() / ".niu" / "user-config.json"
            if not user_config_path.exists():
                # 兼容项目内 config/user-config.json
                user_config_path = Path(__file__).parent.parent.parent / "config" / "user-config.json"
            if not user_config_path.exists():
                return
            with open(user_config_path, encoding="utf-8") as f:
                user_config = json.load(f)
            if not _should_auto_probe_after_upgrade(user_config):
                return  # 已探测过

            # 后台触发探测（用当前 lightrag_llm 配置）
            import httpx
            config = get_llm_config(use_lightrag_config=True)
            # 标准化字段名（get_llm_config 返回小写）
            probe_payload = {
                "apikey": config.get("apikey", ""),
                "apibase": config.get("apibase", ""),
                "model": config.get("model", ""),
                "type": config.get("type", "openai"),
                "provider": config.get("provider", ""),
                "litellm_kwargs": config.get("litellm_kwargs", {}),
            }
            # 重试机制：本函数在 LightRAG eager init 后立即调用，此时 lifespan
            # 可能还没 yield（FastAPI 在 yield 前不处理 HTTP 请求）。daemon 线程
            # 先 sleep 10s 等服务起来，然后最多重试 3 次（每次间隔 10s）。
            data = None
            import time
            time.sleep(10)  # 等 lifespan yield + 服务起来
            for attempt in range(3):
                try:
                    with httpx.Client(timeout=90) as client:
                        resp = client.post(
                            "http://127.0.0.1:9876/api/probe-response-format",
                            json=probe_payload,
                        )
                        data = resp.json()
                    if data.get("result") == "supported":
                        break
                except Exception:
                    pass
                time.sleep(10)
            if not data or data.get("result") != "supported":
                return  # 探测失败不写配置

            mode = data.get("mode")
            if mode not in ("json_schema", "json_object", "prompt_only"):
                return

            # 写入配置（atomic write：先写临时文件再 os.replace，避免主进程
            # 在写入过程中读到部分 JSON 触发 JSONDecodeError）
            allowed = ["response_format"] if mode in ("json_schema", "json_object") else []
            lightrag_llm = user_config.setdefault("lightrag_llm", {})
            litellm_kwargs = lightrag_llm.setdefault("litellm_kwargs", {})
            litellm_kwargs["response_format_mode"] = mode
            litellm_kwargs["allowed_openai_params"] = allowed
            import os
            tmp_path = f"{user_config_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(user_config, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, user_config_path)
            logger.info("Background probe completed: response_format_mode=%s", mode)
        except Exception as e:
            logger.warning("Background probe failed: %s", e)

    threading.Thread(target=_probe_in_background, daemon=True, name="response-format-probe").start()
```

- [ ] **Step 4：在 LightRAG 初始化成功后调用钩子**

找到 `niu_api/internal/lightrag_manager.py` 中 LightRAG 初始化完成的代码（搜 `llm_model_func = _build_llm_model_func()` 在 L919 附近），在其后追加：
```python
    # 升级后首次启动后台探测 response_format 档位（不阻塞启动）
    _trigger_background_probe_if_needed()
```

- [ ] **Step 5：跑测试 + 语法检查**

```bash
./python/bin/python -c "from niu_api.internal.lightrag_manager import _should_auto_probe_after_upgrade, _trigger_background_probe_if_needed; print('OK')"
./python/bin/python -m pytest tests/test_response_format_probe.py -v
```
Expected: 语法 OK + 全部测试 PASS

- [ ] **Step 6：提交**

```bash
git add niu_api/internal/lightrag_manager.py tests/test_response_format_probe.py
git commit -m "feat(lightrag): 升级后首次启动后台自动探测 response_format 档位

旧版本用户配置无 response_format_mode 字段。若不自动探测，GLM 等配置
会永远走 prompt_only（_resolve_response_format 返回 None），与实际能力
矛盾。后台 daemon 线程跑探测，结果写入配置，不阻塞启动。"
```

---

## Task 3: 前端设置窗口接入探测

**Files:**
- Modify: `ui/main/windows/settings/index.html:380-446`（`testAndSave()` 函数）
- Modify: `ui/main/preload-settings.js`
- Modify: `ui/main/main.js`

### Step 3.1：扩展 `testAndSave()` 追加探测调用

- [ ] **Step 1：修改 `ui/main/windows/settings/index.html` 的 `testAndSave()` 函数 L404-444**

将原 `if (result.success) { ... }` 块改为：

```javascript
      if (result.success) {
        // 第一步半：测试通过后追加 response_format 探测（3 档递进）
        setStatus('探测格式化输出能力中（可能需要 30-60 秒）...', 'loading');
        // config 必须包含 litellm_kwargs（从 existingConfig 取），否则探测环境与运行时不一致
        const probeConfig = {
          ...config,
          litellm_kwargs: existingConfig.lightrag_llm?.litellm_kwargs || existingConfig.llm?.litellm_kwargs || {},
        };
        const probeResult = await window.electronAPI.probeResponseFormat(probeConfig);

        let responseFormatMode = 'prompt_only';
        let allowedOpenaiParams = [];
        let probeReason = '';
        let probeFailed = false;

        if (probeResult.result === 'supported') {
          responseFormatMode = probeResult.mode || 'prompt_only';
          allowedOpenaiParams = responseFormatMode === 'prompt_only' ? [] : ['response_format'];
          probeReason = probeResult.reason || `格式化输出档位: ${responseFormatMode}`;
        } else {
          // probe_failed：完全保留旧 litellm_kwargs，不破坏用户已有配置
          probeFailed = true;
          probeReason = '探测失败，保留旧配置: ' + (probeResult.reason || '未知错误');
        }

        // 第二步：保存配置
        setStatus('保存配置中...（' + probeReason + '）', 'loading');
        const existingLightragLlm = existingConfig.lightrag_llm || {};
        const existingLightragKwargs = existingLightragLlm.litellm_kwargs || {};
        // probe_failed 时原样保留旧 litellm_kwargs；supported 时写入探测结果
        const newLitellmKwargs = probeFailed
          ? { ...existingLightragKwargs }
          : {
              ...existingLightragKwargs,
              thinking: existingLightragKwargs.thinking || { type: "disabled" },
              response_format_mode: responseFormatMode,
              allowed_openai_params: allowedOpenaiParams,
            };
        const lightragLlmConfig = {
          presetId: existingLightragLlm.presetId || "",
          apiKey: existingLightragLlm.apiKey || "",
          apiBase: existingLightragLlm.apiBase || "",
          model: existingLightragLlm.model || "",
          type: existingLightragLlm.type || "openai",
          reasoning_effort: existingLightragLlm.reasoning_effort || "none",
          temperature: existingLightragLlm.temperature ?? 0.2,
          litellm_kwargs: newLitellmKwargs,
        };

        const saveResult = await window.electronAPI.saveConfig({
          llm: { ...config, presetId: presetId, reasoning_effort: existingConfig.llm?.reasoning_effort || "" },
          lightrag_llm: lightragLlmConfig,
          context: {
            contextWindowSize: parseInt(document.getElementById('contextWindowSize').value) || 200000,
            warningThreshold: parseFloat(document.getElementById('warningThreshold').value) || 0.8,
            compressTargetTokens: parseInt(document.getElementById('compressTargetTokens').value) || 60000,
            sleepTriggerMinutes: parseInt(document.getElementById('sleepTriggerMinutes').value) || 5
          },
          storage: existingConfig.storage || {},
          firstRun: false
        });
        if (saveResult.success) {
          testPassedInSession = true;
          setStatus('模型测试通过，配置已保存！格式化输出: ' + probeReason, 'success');
          setTimeout(() => { window.electronAPI.closeWindow(); }, 1500);
        } else {
          setStatus('保存失败: ' + saveResult.error, 'error');
        }
      } else {
        setStatus('模型测试失败: ' + (result.message || '请检查配置'), 'error');
      }
```

### Step 3.2：暴露 IPC 接口

- [ ] **Step 2：修改 `ui/main/preload-settings.js`**

参考现有 `testConnection` 暴露方式，在 `electronAPI` 对象内追加：
```javascript
    probeResponseFormat: (config) => ipcRenderer.invoke('probe-response-format', config),
```

- [ ] **Step 3：修改 `ui/main/main.js` 注册 IPC handler**

在 `ipcMain.handle('test-connection', ...)` 之后追加：
```javascript
ipcMain.handle('probe-response-format', async (event, config) => {
  // 通过 HTTP POST 调本机 127.0.0.1:9876/api/probe-response-format
  // 失败时返回 probe_failed，保留旧配置（不破坏用户已有 response_format_mode）
  try {
    const http = require('http');
    const payload = JSON.stringify(config);
    const options = {
      hostname: '127.0.0.1',
      port: 9876,
      path: '/api/probe-response-format',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(payload),
      },
      timeout: 70000,  // 70 秒（两档各 30s + 余量）
    };
    return await new Promise((resolve) => {
      const req = http.request(options, (res) => {
        let data = '';
        res.on('data', (chunk) => data += chunk);
        res.on('end', () => {
          try { resolve(JSON.parse(data)); }
          catch { resolve({ result: 'probe_failed', reason: '响应解析失败', mode: null, raw_response: data.slice(0, 200) }); }
        });
      });
      req.on('error', (e) => resolve({ result: 'probe_failed', reason: e.message, mode: null, raw_response: '' }));
      req.on('timeout', () => { req.destroy(); resolve({ result: 'probe_failed', reason: 'HTTP 超时', mode: null, raw_response: '' }); });
      req.write(payload);
      req.end();
    });
  } catch (e) {
    return { result: 'probe_failed', reason: String(e), mode: null, raw_response: '' };
  }
});
```

### Step 3.3：手动验证前端流程

- [ ] **Step 4：启动程序，打开设置窗口手动测试**

用户已启动程序（`./niu`），打开设置窗口。

验证步骤（豆包 Coding Plan 配置）：
1. 设置窗口填入豆包 Coding Plan 配置（apiBase=`https://ark.cn-beijing.volces.com/api/coding/v3`，model=`ark-code-latest`，provider=`volcengine`，litellm_kwargs.thinking=`{type:enabled}` 主LLM）
2. 点"测试连接并保存"
3. 状态文字应依次显示：
   - "测试模型连接中..."
   - "探测格式化输出能力中（可能需要 30-60 秒）..."
   - "保存配置中...（Tier 1（model_rejected）+ Tier 2（model_rejected）均失败，降级到 prompt-only 模式）"
   - "模型测试通过，配置已保存！格式化输出: ..."
4. 检查 `config/user-config.json`：
   - `lightrag_llm.litellm_kwargs.response_format_mode` 应为 `"prompt_only"`
   - `lightrag_llm.litellm_kwargs.allowed_openai_params` 应为 `[]`
5. 关闭程序，重新打开，验证配置持久化

- [ ] **Step 5：用 GLM 配置验证**

关闭程序，备份当前 `config/user-config.json`，用 `config/user-config - glm.json` 替换。重新启动，打开设置窗口：
1. 设置窗口填入 GLM 配置（apiBase=`https://maas-coding-api.cn-huabei-1.xf-yun.com/v2`，model=`xopglm5`，provider=`openai`）
2. 点"测试连接并保存"
3. 状态应显示：
   - "保存配置中...（Tier 1（gateway_blocked）+ Tier 2（gateway_blocked）均失败，降级到 prompt-only 模式）"
4. 检查 `config/user-config.json`：
   - `lightrag_llm.litellm_kwargs.response_format_mode` 应为 `"prompt_only"`（GLM 实测输出漂移）
   - `lightrag_llm.litellm_kwargs.allowed_openai_params` 应为 `[]`

- [ ] **Step 6：恢复原豆包 Coding Plan 配置**

```bash
# 用户原配置已备份，恢复
git checkout config/user-config.json
```

- [ ] **Step 7：提交**

```bash
git add ui/main/windows/settings/index.html ui/main/preload-settings.js ui/main/main.js
git commit -m "feat(ui): 设置窗口测试连接后自动探测 response_format 档位

豆包 Coding Plan 网关 400 拒绝 response_format，GLM 网关接受但模型输出漂移。
原硬编码默认写入 [\"response_format\"] 导致运行时静默降级。现测试通过后
追加 3 档递进探测，根据结果写入 response_format_mode + allowed_openai_params：
- json_schema strict → [\"response_format\"]
- json_object → [\"response_format\"]
- prompt_only → []
- probe_failed → 完全保留旧 litellm_kwargs（不破坏用户已有配置）

probe_failed 时原样保留旧 litellm_kwargs，避免升级后首次测试失败即静默降级。
独立调用 /api/probe-response-format，不影响 /api/test-llm 启动器复用流程。"
```

---

## Task 4: MCP 工具返回值扩展 + 文档

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:505-521`
- Modify: `docs/manual-user-guide.md` 1.2 节

### Step 4.1：`get_lightrag_llm_config` 返回 `litellm_kwargs`

- [ ] **Step 1：修改 `mcp-servers/config-manager/src/niu_config_manager/__init__.py:505-521`**

将：
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
```

改为：
```python
def get_lightrag_llm_config() -> dict[str, Any]:
    """Get LightRAG LLM configuration (without API key for security).

    Returns the lightrag_llm section if configured, otherwise indicates
    it will fall back to the llm section. Includes litellm_kwargs so the
    main agent can inspect response_format_mode (probe result) and other
    provider-specific params.
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
        "temperature": lightrag_llm.get("temperature", 0.2),
        "litellm_kwargs": lightrag_llm.get("litellm_kwargs", {}),
    }
```

- [ ] **Step 2：语法检查**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/mcp-servers/config-manager/src
REDACTED_USER_PATH/tools/ai-bot/python/bin/python -c "from niu_config_manager import get_lightrag_llm_config; print('OK')"
```
Expected: 输出 `OK`

- [ ] **Step 3：提交**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py
git commit -m "feat(config-manager): get_lightrag_llm_config 返回 litellm_kwargs 字段

主 Agent 通过 MCP 查询配置时可看到 response_format_mode（探测档位）、
allowed_openai_params 和其他厂商特有参数，便于排查知识图谱生成问题。"
```

### Step 4.2：补充用户文档

- [ ] **Step 4：修改 `docs/manual-user-guide.md` 1.2 节**

在 1.2 节"火山方舟(Ark)端点配置说明"之后，"火山方舟深度思考模型 + 工具调用配置"之前，新增小节：

```markdown
**格式化输出（response_format）能力自动探测**

设置窗口"测试连接并保存"按钮在测试通过后，会自动追加一次格式化输出能力探测，按 3 档递进调用真实 LLM，找出当前配置支持的最强档位：

| 档位 | response_format.type | 约束 | 典型支持厂商 |
|------|---------------------|------|--------------|
| Tier 1（最强） | `json_schema` strict | 输出严格匹配 schema | OpenAI 真正支持 |
| Tier 2（中等） | `json_object` | 输出合法 JSON，不约束字段 | OpenAI、DeepSeek |
| Tier 3（最弱） | 无 response_format | prompt + `json_repair` 客户端容错 | 所有厂商兜底 |

**探测流程**：从 Tier 1 开始测，失败则降级测 Tier 2，再失败则定为 Tier 3。每档失败条件：
- `model_rejected`：LiteLLM 抛 `BadRequestError`/`UnsupportedParamsError`（模型/网关 4xx 拒绝，如豆包 Coding Plan）
- `gateway_blocked`：网关返回 200 但响应非合法 JSON（网关接受参数但模型输出漂移，如 GLM xopglm5）

**写入配置**：
- `lightrag_llm.litellm_kwargs.response_format_mode`：`"json_schema"` / `"json_object"` / `"prompt_only"`
- `lightrag_llm.litellm_kwargs.allowed_openai_params`：前两档 `["response_format"]`，prompt_only 档 `[]`（双写兼容旧逻辑）

**典型场景**（2026-07-19 实测）：
- 豆包 Coding Plan 端点（`/api/coding/v3`，model=`ark-code-latest`）：网关 400 拒绝 response_format，探测结果 `prompt_only`
- GLM 端点（`maas-coding-api.cn-huabei-1.xf-yun.com/v2`，model=`xopglm5`）：网关接受但模型输出漂移，探测结果 `prompt_only`
- OpenAI 官方：探测结果 `json_schema`

**升级后自动探测**：旧版本用户配置无 `response_format_mode` 字段。程序启动后若检测到该字段缺失，后台自动触发一次探测写入配置，不阻塞启动。

**手动覆盖**：关闭程序后手动编辑 `config/user-config.json` 的 `lightrag_llm.litellm_kwargs.response_format_mode`。下次设置窗口测试保存会覆盖手动值。

**注意**：
- 探测调用会消耗约 100-200 token（最坏情况测到 Tier 2）。仅"测试连接并保存"按钮触发，不引入后台定时探测（升级后首次启动除外）。
- 探测独立于启动时的 LLM 连通性测试（`/api/test-llm`），不影响启动速度。
```

- [ ] **Step 5：提交**

```bash
git add docs/manual-user-guide.md
git commit -m "docs: 补充格式化输出能力 3 档递进探测说明（含真实环境实测）"
```

---

## Task 5: 集成测试 + 真实程序验证

**Files:**
- Test: `tests/test_response_format_probe.py` 追加端到端集成测试

### Step 5.1：集成测试 — 端点 e2e

- [ ] **Step 1：追加集成测试到 `tests/test_response_format_probe.py`**

```python
"""端到端集成测试：调用 /api/probe-response-format 端点。

需启动 niu_api 服务（端口 9876）。验证三种探测档位路径 + 两个真实配置。
"""
import json
import os
import shutil
import pytest
import httpx


@pytest.fixture
def api_base():
    return "http://127.0.0.1:9876"


def test_probe_endpoint_returns_json_schema_for_openai(api_base):
    """用 OpenAI 真实 API Key 测试（需环境变量 OPENAI_API_KEY）"""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        pytest.skip("OPENAI_API_KEY 未设置，跳过真实 OpenAI 探测测试")
    config = {
        "apiKey": api_key,
        "apiBase": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "type": "openai",
        "provider": "",
    }
    with httpx.Client(timeout=90) as client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] in {"supported", "probe_failed"}
    # OpenAI 应支持 json_schema strict
    assert data["mode"] == "json_schema", f"OpenAI 应支持 json_schema，实际: {data}"


def test_probe_endpoint_returns_prompt_only_for_doubao_coding(api_base):
    """用豆包 Coding Plan 真实配置测试

    用户已启动程序，config/user-config.json 即豆包 Coding Plan 配置。
    直接读 config 文件取真实 API Key 避免环境变量依赖。

    重要：litellm_kwargs 用 lightrag_llm 段（thinking={type:disabled}），
    与运行时 get_llm_config(use_lightrag_config=True) fallback 逻辑一致。
    如果用 llm 段 thinking={type:enabled}，豆包模型走深度思考可能输出
    reasoning_content 无文本 chunk，被判 gateway_blocked 而非 model_rejected，
    与真实环境验证报告结论不一致。
    """
    config_path = "REDACTED_USER_PATH/tools/ai-bot/config/user-config.json"
    if not os.path.exists(config_path):
        pytest.skip("豆包配置文件不存在")
    with open(config_path) as f:
        user_cfg = json.load(f)
    llm = user_cfg.get("llm", {})
    lightrag_llm = user_cfg.get("lightrag_llm", {})
    if not llm.get("apiKey"):
        pytest.skip("豆包配置文件无 apiKey")

    # 用 lightrag_llm 段的 litellm_kwargs（thinking=disabled），与运行时一致
    # lightrag_llm.model 为空时，运行时 get_llm_config 走 fallback 用 llm 段
    # apiKey/apiBase/model，但 litellm_kwargs 用 lightrag_llm 段
    config = {
        "apiKey": llm["apiKey"],
        "apiBase": llm["apiBase"],
        "model": llm["model"],
        "type": llm.get("type", "openai"),
        "provider": llm.get("provider", ""),
        "litellm_kwargs": lightrag_llm.get("litellm_kwargs", {}),
    }
    with httpx.Client(timeout=90) as client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # 豆包 Coding Plan 网关 400 拒绝 response_format，应降级 prompt_only
    assert data["mode"] == "prompt_only", f"豆包 Coding Plan 应降级 prompt_only，实际: {data}"
    # reason 应含 model_rejected（豆包网关返回 BadRequestError）
    assert "model_rejected" in data.get("reason", ""), \
        f"豆包应触发 model_rejected（网关 400），实际 reason: {data.get('reason')}"


def test_probe_endpoint_returns_prompt_only_for_glm(api_base):
    """用 GLM 真实配置测试

    config/user-config - glm.json 是 GLM 配置，实测网关接受 response_format
    但模型输出漂移（含额外字符），json.loads 失败，应降级 prompt_only。
    """
    config_path = "REDACTED_USER_PATH/tools/ai-bot/config/user-config - glm.json"
    if not os.path.exists(config_path):
        pytest.skip("GLM 配置文件不存在")
    with open(config_path) as f:
        user_cfg = json.load(f)
    llm = user_cfg.get("llm", {})
    if not llm.get("apiKey"):
        pytest.skip("GLM 配置文件无 apiKey")

    config = {
        "apiKey": llm["apiKey"],
        "apiBase": llm["apiBase"],
        "model": llm["model"],
        "type": llm.get("type", "openai"),
        "provider": llm.get("provider", ""),
        "litellm_kwargs": {"thinking": {"type": "disabled"}},  # GLM 入库配置
    }
    with httpx.Client(timeout=90) as client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # GLM 网关接受 response_format 但模型输出漂移，应降级 prompt_only
    assert data["mode"] == "prompt_only", f"GLM 应降级 prompt_only（输出漂移），实际: {data}"
    # reason 应含 gateway_blocked（GLM 网关 200 接受但输出非合法 JSON）
    assert "gateway_blocked" in data.get("reason", ""), \
        f"GLM 应触发 gateway_blocked（输出漂移），实际 reason: {data.get('reason')}"


def test_probe_endpoint_returns_probe_failed_for_invalid_config(api_base):
    """无效配置（缺 apikey）应返回 probe_failed"""
    config = {"apiKey": "", "apiBase": "", "model": ""}
    with httpx.Client(timeout=10) as client:
        resp = client.post(f"{api_base}/api/probe-response-format", json=config)
    assert resp.status_code == 200
    data = resp.json()
    assert data["result"] == "probe_failed"
    assert data["mode"] is None


def test_probe_endpoint_does_not_affect_test_llm_endpoint(api_base):
    """验证 /api/test-llm 响应结构未被探测逻辑污染（启动器依赖 {success, message, error}）"""
    config = {"apiKey": "fake-key", "apiBase": "https://api.openai.com/v1", "model": "gpt-4o-mini"}
    with httpx.Client(timeout=15) as client:
        resp = client.post(f"{api_base}/api/test-llm", json=config)
    assert resp.status_code == 200
    data = resp.json()
    # 必须只有 success/message/error 三字段（启动器 TestLlmResult 结构体依赖）
    assert "success" in data
    # 不能有 result/mode/raw_response 等探测字段
    assert "result" not in data
    assert "mode" not in data
```

- [ ] **Step 2：确认 niu_api 服务在线**

用户已启动程序，直接调 `/api/test-llm` 验证：
```bash
curl -s -X POST http://127.0.0.1:9876/api/test-llm -H "Content-Type: application/json" -d '{}' | python3 -m json.tool
```
Expected: 返回 `{"success": true, "message": "..."}`

- [ ] **Step 3：跑集成测试**

```bash
./python/bin/python -m pytest tests/test_response_format_probe.py -v
```
Expected:
- 单元测试全 PASS
- `test_probe_endpoint_returns_probe_failed_for_invalid_config` PASS
- `test_probe_endpoint_does_not_affect_test_llm_endpoint` PASS
- `test_probe_endpoint_returns_prompt_only_for_doubao_coding` PASS（豆包配置真实可用）
- `test_probe_endpoint_returns_prompt_only_for_glm` PASS（GLM 配置真实可用）
- `test_probe_endpoint_returns_json_schema_for_openai` SKIP（无 OPENAI_API_KEY）

### Step 5.2：真实程序端到端验证

- [ ] **Step 4：豆包 Coding Plan 配置完整流程验证**

用户已启动 `./niu`，打开设置窗口：
1. 设置窗口显示当前豆包 Coding Plan 配置
2. 点"测试连接并保存"
3. 等待状态文字依次显示测试→探测（30-60s）→保存→成功
4. 检查 `config/user-config.json`：
   - `lightrag_llm.litellm_kwargs.response_format_mode` 应为 `"prompt_only"`
   - `lightrag_llm.litellm_kwargs.allowed_openai_params` 应为 `[]`
5. 拖入一份测试文档（如 `docs/manual-user-guide.md`）触发知识图谱入库
6. 检查 `logs/raw_http/YYYYMMDD/` 响应日志，验证 keyword_extraction 调用**不构造 response_format**（应走 prompt-only 路径，请求体无 `response_format` 字段）
7. 验证入库不报错、实体提取结果正常

- [ ] **Step 5：GLM 配置完整流程验证**

关闭程序，备份当前配置：
```bash
cp config/user-config.json config/user-config-backup-doubao.json
cp "config/user-config - glm.json" config/user-config.json
```
重新启动 `./niu`，打开设置窗口：
1. 设置窗口显示 GLM 配置（apiBase=`maas-coding-api.cn-huabei-1.xf-yun.com/v2`，model=`xopglm5`，provider=`openai`）
2. 点"测试连接并保存"
3. 等待状态显示探测通过（GLM 模型本身能正常对话）+ 探测降级 prompt_only（response_format 输出漂移）
4. 检查 `config/user-config.json`：
   - `lightrag_llm.litellm_kwargs.response_format_mode` 应为 `"prompt_only"`
5. 拖入测试文档验证入库正常
6. 验证完成后恢复豆包配置：
```bash
cp config/user-config-backup-doubao.json config/user-config.json
```

- [ ] **Step 6：提交集成测试**

```bash
git add tests/test_response_format_probe.py
git commit -m "test(response_format_probe): 端到端集成测试覆盖 3 档递进 + 两个真实配置

- OpenAI 真实 API → json_schema
- 豆包 Coding Plan 真实配置 → prompt_only（网关 400 拒绝）
- GLM 真实配置 → prompt_only（网关接受但模型输出漂移）
- 无效配置 → probe_failed
- /api/test-llm 响应结构未被污染（启动器依赖不变）

真实 LLM 调用验证探测逻辑正确，符合 [[real-testing-only]] 铁律。
两个真实配置文件均覆盖，符合用户要求。"
```

### Step 5.3：清理 + 最终汇报

- [ ] **Step 7：杀掉测试进程**

```bash
# 用户已启动程序，验证完毕后可让用户决定是否关闭
# 如需关闭，用 kill -TERM 优雅退出，禁止 pkill -f niu（[[test-process-kill-corruption]]）
ps aux | grep "niu\|python -m niu_api" | grep -v grep | awk '{print $2}' | xargs -r kill -TERM
```

- [ ] **Step 8：最终汇报**

向用户汇报：
- 实施完成的 commit 列表
- 探测 3 档递进逻辑：json_schema → json_object → prompt_only
- 真实环境验证结论（推翻 v1 假设）：
  - 豆包 Coding Plan：网关 400 拒绝（非静默剥离）→ prompt_only
  - GLM：网关接受但模型输出漂移 → prompt_only
- `/api/test-llm` 未被改动（启动器复用零影响）
- 升级后首次启动后台探测机制
- 入库质量对比（prompt-only 模式入库正常，不报错）

---

## Self-Review 清单

### Spec 覆盖
- ✅ 设置窗口测试连接时自动探测 → Task 3
- ✅ 从最优方案开始测试，不支持再降级 → Task 2（3 档递进）
- ✅ 两种失败模式都覆盖 → Task 2（`model_rejected` 网关 4xx + `gateway_blocked` 网关 200+非JSON）
- ✅ 最终保底方案 prompt-only → Task 2 Tier 3 + Task 1 `prompt_only` mode 返回 None
- ✅ LightRAG 运行时根据配置决定构造哪种 response_format → Task 1 `_resolve_response_format`
- ✅ 不污染启动器复用的 `/api/test-llm` → Task 2 独立端点 + Task 5 集成测试断言
- ✅ MCP 工具可查询当前状态 → Task 4.1
- ✅ 文档说明 → Task 4.2
- ✅ 升级后旧配置引导探测 → Task 2.5
- ✅ 两个真实配置测试案例 → Task 5（豆包 Coding Plan + GLM）

### 占位符扫描
- ✅ 无 TBD/TODO
- ✅ 所有步骤含完整代码块
- ✅ 所有命令含 expected 输出
- ✅ 函数签名一致：`_resolve_response_format(config)` / `_should_auto_probe_after_upgrade(config)` / `_trigger_background_probe_if_needed()` / `_classify_probe_response_tier1(text)` / `_classify_probe_response_tier2(text)` / `_build_probe_response_format_json_schema()` / `_build_probe_response_format_json_object()` / `_build_probe_messages()`

### 类型一致性
- ✅ `response_format_mode` 取值：`"json_schema"` / `"json_object"` / `"prompt_only"`（全文档一致）
- ✅ 探测返回 `result` 字段值：`"supported"` / `"probe_failed"`（Tier 3 也返回 `"supported"` + `mode="prompt_only"`，全文档一致）
- ✅ 配置字段名 `lightrag_llm.litellm_kwargs.response_format_mode` + `allowed_openai_params`（双写，全文档一致）

### 风险再评估
1. **Task 1 Step 5 移动 `config = get_llm_config(...)` 位置**：原 L158 提前到 L132 之前。`get_llm_config(use_lightrag_config=True)` 在 `_llm_model_func` 内调用是 async 函数内的同步调用，与现有 L158 调用一致，无新风险。删除原 L158 那行避免重复调用。
2. **Task 1 Step 6 `test_keyword_extraction_builds_response_format` 测试需更新 fixture**：该测试 mock config 不含 `response_format_mode` 也不含 `allowed_openai_params`，新逻辑会让 `response_format=None`，现有断言 FAIL。Step 6 已明确要求给 mock config 加 `"litellm_kwargs": {"response_format_mode": "json_schema"}`。
3. **Task 2 探测复用 `LiteLLMSession.chat`**（v1 是绕过直接调 `litellm.completion`）：v2 修正，确保探测和运行时 kwargs 完全一致（含 `drop_params=True`、`stream=True`、`temperature` 等）。`LiteLLMSession.chat` 在传 response_format 时强制 `drop_params=True`（L368-370），但 `litellm_kwargs.allowed_openai_params=["response_format"]` 让 LiteLLM 真正透传 response_format 到 provider 网关，不会静默丢弃。这是真实环境验证确认的路径。
4. **Task 2 Tier 3 也返回 `"result": "supported"`**：语义上 Tier 3 是"探测成功完成，找到了合适的档位是 prompt_only"，不是"探测失败"。仅当探测本身异常（超时/网络/API Key 错）才返回 `"probe_failed"`。前端 Task 3 据此正常保存，不破坏用户已有配置。
5. **Task 3 前端 `probe_failed` 时原样保留 `litellm_kwargs`**（v1 bug 修正）：`newLitellmKwargs = probeFailed ? {...existingLightragKwargs} : {...覆盖}`，确保升级后首次测试失败不破坏用户已有配置。
6. **Task 2.5 后台探测失败不影响启动**：`_trigger_background_probe_if_needed` 在 daemon 线程跑，所有异常被 try/except 包住，仅记录 warning 日志。
7. **Task 5 真实配置测试**：豆包 + GLM 两个真实配置文件均作为集成测试 case，符合 [[real-testing-only]] 铁律。用户已启动程序，集成测试可现场跑。
8. **Task 5 `test_probe_endpoint_does_not_affect_test_llm_endpoint`**：专门验证 `/api/test-llm` 响应结构不含 `result`/`mode`/`raw_response` 字段，保证启动器 `TestLlmResult` 结构体解析不受影响。
9. **`allowed_openai_params` 探测时强制写入 `["response_format"]`**（v1 没明确）：v2 Step 5 明确 `probe_litellm_kwargs["allowed_openai_params"] = ["response_format"]`，让 LiteLLM volcengine router 透传 response_format，否则请求未发出抛 `UnsupportedParamsError`。
