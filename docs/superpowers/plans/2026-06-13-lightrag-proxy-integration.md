# LightRAG LLM 调用统一走代理 — 实施计划 v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 LightRAG 的 LLM 调用从 OpenAI SDK 直连改为走我们的统一代理（`localhost:9876/llm/v1`），同时在代理层处理 `response_format` 不支持的降级，使 LightRAG 在任何模型下都能正常工作。

**Architecture:** 1) 改造 `lightrag_manager.py` 的 `_llm_model_func`，不再用 `openai_complete_if_cache`，改为直接 HTTP 调代理；2) 在 `llm_proxy.py` 添加 `response_format` 降级逻辑——探测模型支持能力并缓存，不支持时去掉 `response_format` 改为 prompt 注入 JSON 格式要求；3) 代理端始终返回完整 JSON（stream 参数被忽略），`_llm_model_func` 需要将 JSON 响应转为 LightRAG 期望的 `str` 或 `AsyncIterator[str]` 格式。

**Tech Stack:** Python, httpx, litellm 1.88.1, LightRAG fork (1.4.16)

**关键架构事实（代理端现状）：**
- `chat_completions` 端点**始终返回完整 JSON**（`OpenAIChatResponse`），`stream` 参数被忽略
- `call_llm_via_litellm` 始终同步消费整个 LiteLLM 流，拼接为 `full_text`
- `reasoning_content`（COT）在 `MockResponse.thinking` 中，但 HTTP 响应中**被丢弃**
- LightRAG 请求默认 `reasoning_effort="none"`，所以当前没有 COT 内容
- `response_format` 目前直接透传给 LiteLLM

---

## File Structure

| File | Responsibility |
|------|---------------|
| `tests/test_response_format_handler.py` | Task 1 的单元测试 |
| `niu_api/internal/response_format_handler.py` | 新文件：`response_format` 降级核心逻辑（缓存、prompt 注入、JSON 提取） |
| `niu_api/internal/lightrag_manager.py` | 改造 `_llm_model_func`：从 OpenAI SDK 直连改为 HTTP 调代理 |
| `niu_api/llm_proxy.py` | 添加 `response_format` 降级集成：探测 + 降级决策 + JSON 提取 |
| `test_response_format_integration.py` | Task 4 的真实 LLM 集成测试（临时文件，测试后删除） |

---

### Task 1: response_format_handler.py — 缓存 + prompt 注入 + JSON 提取（TDD）

**Files:**
- Create: `tests/test_response_format_handler.py`
- Create: `niu_api/internal/response_format_handler.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_response_format_handler.py`：

```python
"""response_format_handler 单元测试。"""
import json
import pytest
from niu_api.internal.response_format_handler import (
    get_cached_capabilities,
    set_cached_capabilities,
    clear_capability_cache,
    _build_injection_messages,
    downgrade_response_format,
    extract_json_from_response,
)


class TestCapabilityCache:
    def setup_method(self):
        clear_capability_cache()

    def test_cache_empty_by_default(self):
        assert get_cached_capabilities("any-model") is None

    def test_set_and_get(self):
        set_cached_capabilities("model-a", json_schema=True, json_object=False)
        caps = get_cached_capabilities("model-a")
        assert caps == {"json_schema": True, "json_object": False}

    def test_clear_cache(self):
        set_cached_capabilities("model-a", json_schema=True, json_object=True)
        clear_capability_cache()
        assert get_cached_capabilities("model-a") is None

    def test_multiple_models(self):
        set_cached_capabilities("model-a", json_schema=True, json_object=True)
        set_cached_capabilities("model-b", json_schema=False, json_object=False)
        assert get_cached_capabilities("model-a")["json_schema"] is True
        assert get_cached_capabilities("model-b")["json_schema"] is False


class TestBuildInjectionMessages:
    def test_json_schema_injection(self):
        fmt = {
            "type": "json_schema",
            "json_schema": {
                "name": "GPTKeywordExtractionFormat",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "high_level_keywords": {"type": "array", "items": {"type": "string"}},
                        "low_level_keywords": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["high_level_keywords", "low_level_keywords"],
                    "additionalProperties": False,
                },
            },
        }
        msgs = _build_injection_messages(fmt)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        content = msgs[0]["content"]
        assert "JSON" in content
        assert "high_level_keywords" in content
        assert "low_level_keywords" in content

    def test_json_object_injection(self):
        fmt = {"type": "json_object"}
        msgs = _build_injection_messages(fmt)
        assert len(msgs) == 1
        assert "JSON" in msgs[0]["content"]

    def test_downgrade_needed_json_schema_unsupported(self):
        clear_capability_cache()
        set_cached_capabilities("test-model", json_schema=False, json_object=False)
        fmt = {"type": "json_schema", "json_schema": {"name": "test", "strict": True, "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}}}
        result = downgrade_response_format(fmt, "test-model")
        assert result is not None
        assert len(result) == 1

    def test_no_downgrade_when_supported(self):
        clear_capability_cache()
        set_cached_capabilities("test-model", json_schema=True, json_object=True)
        fmt = {"type": "json_schema", "json_schema": {"name": "test", "strict": True, "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}}}
        result = downgrade_response_format(fmt, "test-model")
        assert result is None

    def test_no_downgrade_when_no_cache(self):
        clear_capability_cache()
        fmt = {"type": "json_schema", "json_schema": {"name": "test", "strict": True, "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}}}
        result = downgrade_response_format(fmt, "unknown-model")
        assert result is None

    def test_downgrade_none_format(self):
        result = downgrade_response_format(None, "any-model")
        assert result is None


class TestExtractJsonFromResponse:
    def test_pure_json(self):
        assert extract_json_from_response('{"a": 1}') == '{"a": 1}'

    def test_json_with_prefix_text(self):
        result = extract_json_from_response('Here is the result: {"high_level_keywords": ["AI"], "low_level_keywords": ["助手"]}')
        assert '"high_level_keywords"' in result
        parsed = json.loads(result)
        assert "high_level_keywords" in parsed

    def test_json_with_think_tags(self):
        content = 'Some thinking\n{"a": 1}'
        result = extract_json_from_response(content)
        assert '"a"' in result

    def test_invalid_json_returns_original(self):
        result = extract_json_from_response("just plain text no json")
        assert result == "just plain text no json"


class TestProxyDowngradeIntegration:
    """模拟 llm_proxy 中降级路径的单元测试（不依赖真实 LLM）。"""

    def setup_method(self):
        clear_capability_cache()

    def test_downgrade_json_schema_unsupported(self):
        """模型不支持 json_schema 时，降级应返回注入消息。"""
        set_cached_capabilities("unsupported-model", json_schema=False, json_object=False)
        fmt = {
            "type": "json_schema",
            "json_schema": {
                "name": "GPTKeywordExtractionFormat",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "high_level_keywords": {"type": "array", "items": {"type": "string"}},
                        "low_level_keywords": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["high_level_keywords", "low_level_keywords"],
                    "additionalProperties": False,
                },
            },
        }
        injection = downgrade_response_format(fmt, "unsupported-model")
        assert injection is not None
        # 注入消息应为 user role
        assert injection[0]["role"] == "user"
        # 注入消息应包含关键字段名
        assert "high_level_keywords" in injection[0]["content"]
        assert "low_level_keywords" in injection[0]["content"]

    def test_downgrade_json_object_unsupported(self):
        """模型不支持 json_object 但支持 json_schema 时，json_object 应降级。"""
        set_cached_capabilities("partial-model", json_schema=True, json_object=False)
        fmt = {"type": "json_object"}
        injection = downgrade_response_format(fmt, "partial-model")
        assert injection is not None

    def test_no_downgrade_json_schema_supported(self):
        """模型支持 json_schema 时，不应降级。"""
        set_cached_capabilities("full-model", json_schema=True, json_object=True)
        fmt = {
            "type": "json_schema",
            "json_schema": {"name": "test", "strict": True, "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}},
        }
        assert downgrade_response_format(fmt, "full-model") is None

    def test_extract_json_from_downgraded_response(self):
        """降级后 LLM 可能返回带前缀的 JSON，应能提取。"""
        content = 'Based on the text, here are the keywords:\n{"high_level_keywords": ["AI助手", "知识管理"], "low_level_keywords": ["妞妞", "文档管理"]}'
        extracted = extract_json_from_response(content)
        parsed = json.loads(extracted)
        assert "high_level_keywords" in parsed
        assert len(parsed["high_level_keywords"]) == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_handler.py -v`

Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: 实现 response_format_handler.py 完整代码**

创建 `niu_api/internal/response_format_handler.py`：

```python
"""response_format 降级处理器。

当后端模型不支持 response_format（json_schema / json_object）时，
自动降级为 prompt 注入方式，确保 LLM 仍返回 JSON 格式输出。
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("niu_api.response_format_handler")

_capability_cache: Dict[str, Dict[str, bool]] = {}


def get_cached_capabilities(model: str) -> Optional[Dict[str, bool]]:
    return _capability_cache.get(model)


def set_cached_capabilities(model: str, json_schema: bool, json_object: bool):
    _capability_cache[model] = {"json_schema": json_schema, "json_object": json_object}
    logger.info(f"[ResponseFormat] Cached capabilities for {model}: json_schema={json_schema}, json_object={json_object}")


def clear_capability_cache():
    _capability_cache.clear()
    logger.info("[ResponseFormat] Capability cache cleared")


def _build_injection_messages(response_format: Dict[str, Any]) -> List[Dict[str, str]]:
    """根据 response_format 构建 prompt 注入消息。

    使用 user role 而非 system role，避免多条 system message 在某些 LLM 上不兼容。
    """
    fmt_type = response_format.get("type", "")
    instruction = "\n\nIMPORTANT: You must respond with ONLY a valid JSON object, no other text."

    if fmt_type == "json_schema":
        schema_info = response_format.get("json_schema", {})
        schema_name = schema_info.get("name", "response")
        schema = schema_info.get("schema", {})
        if schema:
            required = schema.get("required", [])
            properties = schema.get("properties", {})
            prop_desc = ", ".join(
                f'"{k}": {v.get("type", "string")}' for k, v in properties.items()
            )
            instruction += (
                f"\nThe JSON object must have these fields: {prop_desc}."
                f"\nRequired fields: {required}."
                f"\nExample: {json.dumps({k: _example_value(v) for k, v in properties.items()}, ensure_ascii=False)}"
            )
        instruction += f"\nSchema name: {schema_name}."

    elif fmt_type == "json_object":
        instruction += "\nReturn a valid JSON object."

    return [{"role": "user", "content": instruction}]


def _example_value(prop: Dict[str, Any]) -> Any:
    t = prop.get("type", "string")
    if t == "array":
        return ["..."]
    if t in ("number", "integer"):
        return 0
    if t == "boolean":
        return True
    return "..."


def downgrade_response_format(
    response_format: Dict[str, Any],
    model: str,
) -> Optional[List[Dict[str, str]]]:
    """判断是否需要降级 response_format。

    Returns:
        None: 不需要降级
        List[dict]: 需要降级，返回追加到 messages 的 prompt 注入消息列表
    """
    if response_format is None:
        return None

    fmt_type = response_format.get("type", "")
    caps = get_cached_capabilities(model)
    if caps is not None:
        if fmt_type == "json_schema" and not caps["json_schema"]:
            return _build_injection_messages(response_format)
        if fmt_type == "json_object" and not caps["json_object"]:
            return _build_injection_messages(response_format)
        return None
    return None


def extract_json_from_response(content: str) -> str:
    """从 LLM 响应中提取 JSON。

    当 response_format 被降级后，LLM 可能返回包含非 JSON 文本的响应。
    此函数尝试提取其中的 JSON 部分。
    """
    try:
        json.loads(content)
        return content
    except json.JSONDecodeError:
        pass

    start = content.find("{")
    end = content.rfind("}") + 1
    if start != -1 and end > start:
        candidate = content[start:end]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    cleaned = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    cleaned = cleaned.strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}") + 1
    if start != -1 and end > start:
        candidate = cleaned[start:end]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return content
```

- [ ] **Step 4: 运行测试确认全部通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_handler.py -v`

Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add niu_api/internal/response_format_handler.py tests/test_response_format_handler.py
git commit -m "feat: add response_format downgrade handler with cache+prompt injection+json extraction (TDD)"
```

---

### Task 2: 改造 lightrag_manager.py — _llm_model_func 改为 HTTP 调代理

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py`

核心改动：去掉 `openai_complete_if_cache` 依赖，改为直接用 httpx 调代理。

**设计原则**：`_llm_model_func` 只做格式转换，不改变任何行为。
- 代理端始终返回完整 JSON，`stream` 参数被忽略
- LightRAG 传 `stream=True` 时，`_llm_model_func` 需返回 `AsyncIterator[str]`（按字符/块 yield 完整内容）
- LightRAG 传 `stream=False` 或不传时，返回 `str`
- `enable_cot` 由代理层处理（LightRAG 默认 `reasoning_effort="none"`，当前无 COT）
- `keyword_extraction=True` 时构建 `response_format` dict 传给代理

- [ ] **Step 1: 修改 import 和全局变量**

在 `lightrag_manager.py` 中：
- 删除 `from lightrag.llm.openai import openai_complete_if_cache`
- 添加 `import httpx`
- 保留 `PROXY_BASE_URL`（`get_lightrag_status()` 中使用，约第 775 行）
- 添加 `PROXY_CHAT_URL = "http://localhost:9876/llm/v1/chat/completions"`

- [ ] **Step 2: 替换 _llm_model_func 实现**

将 `_llm_model_func`（约第 579-588 行）替换为：

```python
PROXY_CHAT_URL = "http://localhost:9876/llm/v1/chat/completions"
# PROXY_API_KEY 已在文件上方定义，直接使用


from typing import AsyncIterator

async def _llm_model_func(
    prompt, system_prompt=None, history_messages=None,
    keyword_extraction=False, **kwargs,
) -> str | AsyncIterator[str]:
    """LightRAG 的 LLM 调用函数，直接走代理而非 OpenAI SDK。

    不再依赖 openai_complete_if_cache 和 OpenAI SDK 的 parse() 方法。
    所有 LLM 调用统一走 localhost:9876 代理，代理层负责处理模型差异。

    返回值：
    - stream=False 或不传：返回 str
    - stream=True：返回 AsyncIterator[str]（将完整内容按块 yield）
    """
    # 提取 stream 参数（决定返回格式）
    stream = kwargs.pop("stream", None)

    # 弹出 LightRAG 内部参数（不传给 HTTP API）
    kwargs.pop("hashing_kv", None)
    kwargs.pop("_priority", None)
    kwargs.pop("keyword_extraction", None)
    kwargs.pop("enable_cot", None)  # 代理层处理，当前 reasoning_effort="none" 无 COT

    # 1. 构建 messages
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if history_messages:
        for msg in history_messages:
            if isinstance(msg, dict) and "role" in msg and "content" in msg:
                messages.append(msg)
    messages.append({"role": "user", "content": prompt})

    # 2. 构建 payload
    payload = {
        "model": "proxy-model",
        "messages": messages,
        "stream": False,
    }

    # 3. 处理 keyword_extraction
    if keyword_extraction:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "GPTKeywordExtractionFormat",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "high_level_keywords": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "low_level_keywords": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["high_level_keywords", "low_level_keywords"],
                    "additionalProperties": False,
                },
            },
        }

    # 4. 发送 HTTP 请求
    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(
                PROXY_CHAT_URL,
                json=payload,
                headers={"Authorization": f"Bearer {PROXY_API_KEY}"},
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"[LightRAG] LLM proxy call failed: {e.response.status_code} {e.response.text[:200]}")
        return ""
    except Exception as e:
        logger.error(f"[LightRAG] LLM proxy call failed: {type(e).__name__}: {str(e)[:200]}")
        return ""

    # 5. 提取返回内容
    choices = data.get("choices", [])
    if not choices:
        return ""

    message = choices[0].get("message", {})
    content = message.get("content", "") or ""

    # 6. 根据是否 stream 返回不同格式
    if stream:
        # LightRAG 期望 AsyncIterator[str]，将完整内容按块 yield
        async def _stream_response():
            chunk_size = 20
            for i in range(0, len(content), chunk_size):
                yield content[i:i + chunk_size]
        return _stream_response()

    return content
```

- [ ] **Step 3: 清理不再需要的代码**

- 删除 `_get_shared_openai_client` 函数（约第 51-70 行）及其全局变量 `_shared_openai_client` 和 `_client_lock`（约第 44-48 行）
- 删除 `from lightrag.llm.openai import openai_complete_if_cache` 的 import
- 检查并清理所有对上述函数/变量的引用

- [ ] **Step 4: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "import py_compile; py_compile.compile('niu_api/internal/lightrag_manager.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 5: 更新测试文件**

检查 `tests/test_lightrag_manager.py`，将 `PROXY_BASE_URL` 的断言改为适配新的变量结构。如果测试 import 了 `PROXY_BASE_URL`，确保它仍然存在（保留用于 `get_lightrag_status`）。

- [ ] **Step 6: Commit**

```bash
git add niu_api/internal/lightrag_manager.py tests/test_lightrag_manager.py
git commit -m "refactor: replace openai_complete_if_cache with direct HTTP proxy call in lightrag_manager"
```

---

### Task 3: 改造 llm_proxy.py — 添加 response_format 降级逻辑

**Files:**
- Modify: `niu_api/llm_proxy.py`

在 `chat_completions` 端点中，当 `response_format` 传过来时，判断后端模型是否支持，不支持则降级为 prompt 注入。

**设计原则**：
- 探测用 `asyncio.to_thread` 包装，避免阻塞事件循环
- 降级注入使用 `user` role 消息，避免多条 `system` 消息的兼容性问题
- 非流式响应中对降级后的内容做 JSON 提取

- [ ] **Step 1: 添加 import**

在 `llm_proxy.py` 文件顶部（import 区域）添加：

```python
from niu_api.internal.response_format_handler import (
    get_cached_capabilities,
    set_cached_capabilities,
    clear_capability_cache,
    downgrade_response_format,
    extract_json_from_response,
)
```

- [ ] **Step 2: 添加探测函数**

在 `call_llm_via_litellm` 函数之前添加：

```python
_probe_lock = asyncio.Lock()


async def _ensure_capabilities(model: str, api_base: str, api_key: str) -> Dict[str, bool]:
    """带锁的探测：避免并发请求触发多次探测。"""
    caps = get_cached_capabilities(model)
    if caps is not None:
        return caps
    async with _probe_lock:
        # 双重检查：获取锁后再次检查缓存
        caps = get_cached_capabilities(model)
        if caps is not None:
            return caps
        return await asyncio.to_thread(
            _probe_response_format_support, model, api_base, api_key
        )


def _probe_response_format_support(model: str, api_base: str, api_key: str) -> Dict[str, bool]:
    """探测模型是否支持 response_format（同步函数，需在 to_thread 中调用）。"""
    import litellm
    json_schema_supported = False
    json_object_supported = False

    try:
        litellm.completion(
            model=f"openai/{model}",
            api_base=api_base,
            api_key=api_key,
            messages=[{"role": "user", "content": "reply with a number"}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "probe",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {"n": {"type": "integer"}},
                        "required": ["n"],
                        "additionalProperties": False,
                    },
                },
            },
            stream=False, timeout=15, max_tokens=5,
        )
        json_schema_supported = True
    except Exception:
        pass

    try:
        litellm.completion(
            model=f"openai/{model}",
            api_base=api_base,
            api_key=api_key,
            messages=[{"role": "user", "content": "reply with a JSON number"}],
            response_format={"type": "json_object"},
            stream=False, timeout=15, max_tokens=5,
        )
        json_object_supported = True
    except Exception:
        pass

    set_cached_capabilities(model, json_schema_supported, json_object_supported)
    return {"json_schema": json_schema_supported, "json_object": json_object_supported}
```

- [ ] **Step 3: 在 chat_completions 中集成降级逻辑**

在 `chat_completions` 函数中，降级逻辑应插入在第 426 行（`# Call LLM` 注释）之前、brain region 注入（第 416 行）之后。这样注入的 user message 与 brain region 注入后的 messages 正确合并。

**重要**：降级逻辑必须在 `call_llm_via_litellm` 调用之前执行，且 `call_llm_via_litellm` 的 `response_format` 参数必须使用降级后的局部变量而非 `request.response_format`。

```python
    # --- response_format 降级处理 ---
    response_format = request.response_format
    was_downgraded = False

    if response_format is not None:
        # 复用 chat_completions 中已有的 is_lightrag 和 config 变量
        # （is_lightrag 在第 398 行、config 在第 401 行已计算）
        model = config["model"]
        api_base = config["apibase"]
        api_key = config["apikey"]

        # 带锁的探测：避免并发请求触发多次探测
        caps = get_cached_capabilities(model)
        if caps is None:
            caps = await _ensure_capabilities(model, api_base, api_key)

        fmt_type = response_format.get("type", "")
        need_downgrade = False

        if fmt_type == "json_schema" and not caps.get("json_schema", False):
            need_downgrade = True
        elif fmt_type == "json_object" and not caps.get("json_object", False):
            need_downgrade = True

        if need_downgrade:
            injection_messages = downgrade_response_format(response_format, model)
            if injection_messages:
                litellm_messages.extend(injection_messages)
            response_format = None
            was_downgraded = True
            logger.info(f"[LLM Proxy] Downgraded response_format for model {model}: {fmt_type} -> prompt injection")
```

同时，将 `call_llm_via_litellm` 调用中的 `response_format=request.response_format` 改为 `response_format=response_format`（使用降级后的局部变量）：

```python
    response = await call_llm_via_litellm(
        messages=litellm_messages,
        tools=litellm_tools,
        response_format=response_format,  # 使用降级后的值（可能为 None）
        config=config,
    )
```

- [ ] **Step 4: 在非流式响应中处理降级后的 JSON 提取**

在响应构建完成后、返回前，如果 `was_downgraded`，对 `openai_response` 中的 content 做 JSON 提取：

```python
    if was_downgraded and openai_response.choices:
        msg = openai_response.choices[0].message
        if msg.content:
            original = msg.content
            extracted = extract_json_from_response(original)
            if extracted != original:
                msg.content = extracted
                logger.debug("[LLM Proxy] Extracted JSON from downgraded response")
```

- [ ] **Step 5: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "import py_compile; py_compile.compile('niu_api/llm_proxy.py', doraise=True); print('OK')"`

Expected: OK

- [ ] **Step 6: Commit**

```bash
git add niu_api/llm_proxy.py
git commit -m "feat: add response_format downgrade in llm_proxy with async probe+cache+prompt injection"
```

---

### Task 4: 集成测试 — 真实 LLM + LightRAG 关键词提取

**Files:**
- Create: `test_response_format_integration.py`（临时测试文件，测试后删除）

- [ ] **Step 1: 编写集成测试脚本**

```python
"""集成测试：验证 LightRAG 关键词提取在 response_format 降级后是否正常工作。"""
import sys
sys.path.insert(0, ".")
import asyncio
import json

from niu_api.internal.lightrag_manager import _llm_model_func


async def test_keyword_extraction():
    """模拟 LightRAG 的关键词提取调用。"""
    prompt = "从以下文本中提取关键词：妞妞是一个智能助手，帮助用户管理文档和知识"
    result = await _llm_model_func(prompt, keyword_extraction=True)
    print(f"LLM 返回原始内容: {result[:200]}")
    try:
        from json_repair import loads
        parsed = loads(result)
        hl = parsed.get("high_level_keywords", [])
        ll = parsed.get("low_level_keywords", [])
        print(f"解析成功! hl={hl}, ll={ll}")
        assert isinstance(hl, list) and isinstance(ll, list)
        print("PASS")
    except Exception as e:
        print(f"FAIL: {e}")
        print(f"原始内容: {result}")


async def test_normal_call():
    """模拟 LightRAG 的普通调用。"""
    prompt = "简单描述一下什么是知识图谱"
    result = await _llm_model_func(prompt, system_prompt="你是一个知识管理助手")
    print(f"普通调用返回: {result[:100]}")
    assert len(result) > 0
    print("PASS")


async def test_stream_call():
    """模拟 LightRAG 的流式查询调用。"""
    prompt = "什么是知识图谱？"
    result = await _llm_model_func(
        prompt,
        system_prompt="你是一个助手",
        stream=True,
    )
    # stream=True 时返回 AsyncIterator
    chunks = []
    async for chunk in result:
        chunks.append(chunk)
    full = "".join(chunks)
    print(f"流式调用返回: {full[:100]}")
    assert len(full) > 0
    print("PASS")


if __name__ == "__main__":
    print("=== 测试 1: 关键词提取 ===")
    asyncio.run(test_keyword_extraction())
    print("\n=== 测试 2: 普通调用 ===")
    asyncio.run(test_normal_call())
    print("\n=== 测试 3: 流式调用 ===")
    asyncio.run(test_stream_call())
```

- [ ] **Step 2: 运行测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python test_response_format_integration.py 2>&1`

Expected: 三个测试都通过

- [ ] **Step 3: 删除测试文件**

```bash
rm test_response_format_integration.py
```

- [ ] **Step 4: Commit（如果有修复）**

```bash
git add -A
git commit -m "fix: adjust response_format downgrade based on integration test results"
```

---

### Task 5: 清理旧依赖和验证

- [ ] **Step 1: 确认 lightrag_manager.py 不再 import openai_complete_if_cache**

Run: `grep -n "openai_complete_if_cache" niu_api/internal/lightrag_manager.py`
Expected: 无匹配

- [ ] **Step 2: 确认其他文件中 openai_complete_if_cache 的使用情况**

Run: `grep -rn "openai_complete_if_cache" niu_api/ agent/ --include="*.py"`
Expected: 仅在 MCP 服务器层（非本次改造范围）

- [ ] **Step 3: 确认 requirements.txt litellm 版本已更新**

Run: `grep "litellm" requirements.txt`
Expected: `litellm==1.88.1`

- [ ] **Step 4: 运行全部单元测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./python/bin/python -m pytest tests/test_response_format_handler.py -v`
Expected: 18 passed

- [ ] **Step 5: 最终提交**

```bash
git add -A
git commit -m "chore: cleanup after LightRAG LLM proxy integration"
```

---

## Self-Review

**1. Spec coverage:**
- LightRAG `_llm_model_func` 改为 HTTP 调代理 → Task 2 ✅
- `response_format` 降级逻辑 → Task 1 + Task 3 ✅
- 探测 + 缓存 → Task 1 + Task 3 ✅
- 模型切换自动失效缓存 → 新模型名不在缓存中，触发重新探测 ✅
- stream=True 返回 AsyncIterator → Task 2 ✅
- COT 处理 → 当前 reasoning_effort="none" 无 COT，代理端丢弃 thinking 不影响 ✅
- 集成测试 → Task 4 ✅
- 清理旧依赖 → Task 5 ✅

**2. Placeholder scan:**
- 无 TBD/TODO
- 所有代码步骤都有完整代码

**3. Type consistency:**
- `_llm_model_func` 返回 `str` 或 `AsyncIterator[str]` → Task 2 ✅
- `downgrade_response_format` 返回 `Optional[List[Dict[str, str]]]` → Task 1 ✅
- `extract_json_from_response` 返回 `str` → Task 1 ✅
- `_build_injection_messages` 使用 `user` role → 避免多条 system message 冲突 ✅
- `_probe_response_format_support` 用 `asyncio.to_thread` 包装 → 不阻塞事件循环 ✅
- `PROXY_BASE_URL` 保留 → `get_lightrag_status` 和测试不受影响 ✅

**4. TDD compliance:**
- Task 1: 先写 14 个失败测试 → 再实现 → 测试通过 ✅
- Task 2: 代码改造（由 Task 4 集成测试覆盖）
- Task 3: 代理层集成（由 Task 4 集成测试覆盖）
- Task 4: 真实 LLM 端到端测试（含 stream 测试）✅
- Task 5: 验证 + 清理 ✅