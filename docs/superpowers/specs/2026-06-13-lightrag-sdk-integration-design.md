# LightRAG LLM 调用统一走 SDK — 设计文档

**Goal:** 将 LightRAG 的 LLM 调用从 OpenAI SDK 直连改为直接调 LiteLLM，消除模型兼容性问题。同时清理不再需要的代理路由，将脑区注入从代理层搬到 `_llm_model_func` 内部。

## 问题根因

LightRAG 的 `openai_complete_if_cache` 使用 OpenAI SDK 专有的 `chat.completions.parse()` + Pydantic 类做 `response_format`（Structured Outputs），非 OpenAI 模型不支持就报错。我们的 Agent 从来没这个问题，因为我们用 LiteLLM，它自动处理各厂商差异。

## 当前架构

```
主Agent对话：前端 -> /chat/sync -> runner.chat() -> LiteLLMSession.chat() -> litellm.completion()  OK 直接SDK

LightRAG LLM：operate.py -> _llm_model_func -> openai_complete_if_cache
  -> OpenAI SDK chat.completions.parse()  <-- 问题根源
  -> HTTP POST localhost:9876/llm/v1/chat/completions
  -> llm_proxy.py -> call_llm_via_litellm -> LiteLLMSession.chat()  <-- 绕了一大圈

LightRAG Embedding：本地 SentenceTransformer，不走代理  OK 已经直接SDK

脑区注入：llm_proxy.py 拦截 LightRAG 请求 -> get_brain_regions() 读内存图 -> 拼入 system_prompt
```

## 改造后架构

```
主Agent对话：不变  OK

LightRAG LLM：operate.py -> _llm_model_func -> LiteLLMSession.chat() -> litellm.completion()  OK 直接SDK

LightRAG Embedding：不变  OK

脑区注入：_llm_model_func 内部 -> get_brain_regions() 读内存图 -> 拼入 system_prompt
```

代理程序路由：删除 `/llm/v1/chat/completions` 和 `/llm/v1/embeddings` 端点（无调用方），保留 `get_llm_config` 和 `call_llm_via_litellm` 函数（被 MCP 客户端引用）。

## 设计原则

1. **应用层只管按 SDK 标准写请求**，模型兼容性由 LiteLLM 处理
2. **代理程序函数逻辑不动**，只删没人用的路由端点
3. **脑区注入搬到 `_llm_model_func` 内部**，用已有的 `get_brain_regions()` 读内存图
4. **只改需要改的地方**，不重构、不扩展、不加新模块

## 具体改动

### 改动 1: `_llm_model_func` — 从 OpenAI SDK 改为直接调 LiteLLM

**文件**: `niu_api/internal/lightrag_manager.py`

**删除的代码：**
- `from lightrag.llm.openai import openai_complete_if_cache`
- `_get_shared_openai_client` 函数及全局变量 `_shared_openai_client`、`_client_lock`
- `PROXY_BASE_URL` 常量（需检查 `get_lightrag_status` 是否还用）
- `PROXY_API_KEY` 常量

**新增的代码：**
- `from agent.generic.litellm_adapter import LiteLLMSession`
- `from niu_api.llm_proxy import get_llm_config`
- `from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt, build_dynamic_brain_region_prompt`

**新的 `_llm_model_func` 逻辑：**

1. 弹出 LightRAG 内部参数：`hashing_kv`、`_priority`（不传给 LiteLLM）
2. 检测是否为实体提取请求（`system_prompt` 包含 `"Knowledge Graph Specialist"`）
   - 是：调 `build_static_brain_region_prompt()` + `build_dynamic_brain_region_prompt()`，拼入 `system_prompt` 末尾
3. 构建 messages 列表（`system_prompt` + `history_messages` + `prompt`）
4. 从 `get_llm_config(use_lightrag_config=True)` 获取 LLM 配置
5. 构建 `LiteLLMSession` 实例
6. 处理 `keyword_extraction`：转成标准 `response_format` 字典传给 `session.chat()`
7. 调用 `session.chat(messages, response_format=..., stream=True)`
8. 消费 Generator 获取完整响应（`MockResponse`）
9. 处理 `enable_cot`：从 `MockResponse.thinking` 提取思考内容，包装成思考标签
10. 根据 `stream` 参数返回 `str` 或 `AsyncIterator[str]`

**关键细节：**

- **配置获取**：`get_llm_config(use_lightrag_config=True)` 返回包含 `model`、`apibase`、`apikey`、`type`、`reasoning_effort` 的字典，传给 `LiteLLMSession` 构造函数
- **keyword_extraction 处理**：`GPTKeywordExtractionFormat` 是 Pydantic BaseModel，需转为标准 `response_format` 字典格式。用 `GPTKeywordExtractionFormat.model_json_schema()` 获取 schema
- **drop_params**：传 `response_format` 时必须设 `drop_params=True`，否则不支持该参数的模型会抛 `UnsupportedParamsError`
- **脑区注入**：复用 `brain_region_prompt.py` 中的 `build_static_brain_region_prompt()` 和 `build_dynamic_brain_region_prompt()`（后者内部调 `get_brain_regions()` 读内存图），拼接到 `system_prompt` 末尾
- **enable_cot 处理**：`LiteLLMSession.chat()` 返回 `Generator[str, None, MockResponse]`，`MockResponse.thinking` 包含 `reasoning_content`。当 `enable_cot=True` 且 thinking 非空且 content 也非空时，只返回 content（LightRAG 的 `openai_complete_if_cache` 也是这个行为）。当 thinking 非空但 content 为空时，返回思考标签包裹的 thinking 内容
- **stream 处理**：`stream=True` 时，消费 Generator 收集所有文本块，然后用 `async def` 生成器按块 yield，返回 `AsyncIterator[str]`
- **LiteLLMSession 实例**：每次调用新建（跟 `call_llm_via_litellm` 一致，因为配置可能动态变化）
- **history_messages 处理**：只检查 `isinstance(msg, dict) and "role" in msg`，允许 `content` 为 `None`（OpenAI 格式中带 `tool_calls` 的 assistant 消息 `content` 可以为 `None`）

### 改动 2: 删除代理程序中没人用的路由端点

**文件**: `niu_api/llm_proxy.py`

- 删除 `chat_completions` 端点函数
- 删除 `/llm/v1/embeddings` 端点函数
- 删除 `is_lightrag_extraction_request` 相关的 import 和调用
- 删除脑区注入拦截代码（`inject_brain_region_context` 调用）
- 保留 `get_llm_config`、`call_llm_via_litellm`、`openai_to_litellm_messages` 等函数（被 MCP 客户端和其他代码引用）
- 保留 `/llm/v1/models`、`/llm/v1/health`、`/llm/v1/status` 等辅助端点
- 保留路由注册（router 对象仍需存在，否则 `__main__.py` 的 import 会报错）

**文件**: `niu_api/__main__.py`

- 检查路由注册是否需要调整

### 改动 3: 清理不再需要的代码

**文件**: `niu_api/internal/lightrag_manager.py`

- 删除 `PROXY_BASE_URL` 和 `PROXY_API_KEY` 常量
- 删除 `_get_shared_openai_client` 函数及全局变量
- 检查 `get_lightrag_status` 函数，如果它用了 `PROXY_BASE_URL`，需要改为直接用 `get_llm_config` 获取状态

**文件**: `niu_api/internal/brain_region_prompt.py`

- `inject_brain_region_context` 函数不再被 `llm_proxy.py` 调用，但保留（`build_static_brain_region_prompt` 和 `build_dynamic_brain_region_prompt` 被 `_llm_model_func` 使用）

## 不改的东西

1. **`agent/generic/litellm_adapter.py`** — 不改，`LiteLLMSession` 原样使用
2. **LightRAG fork 的 `operate.py`、`prompt.py`** — 不改，脑区注入在 `_llm_model_func` 层做
3. **LightRAG Embedding** — 不改，已经走本地模型
4. **`call_llm_via_litellm` 函数** — 保留，被 MCP 客户端的 Sampling 回调引用
5. **`get_llm_config` 函数** — 保留，被 MCP 客户端和新的 `_llm_model_func` 引用

## 涉及的文件清单

| 文件 | 操作 |
|------|------|
| `niu_api/internal/lightrag_manager.py` | 改造 `_llm_model_func`，删除旧代码 |
| `niu_api/llm_proxy.py` | 删除 chat_completions 和 embeddings 端点，删除脑区拦截 |
| `niu_api/__main__.py` | 检查路由注册 |
| `niu_api/internal/brain_region_prompt.py` | 不改，保留供 `_llm_model_func` 调用 |

## 验证标准

1. LightRAG 实体提取正常工作（新文档入库成功）
2. `keyword_extraction` 在不支持 `response_format` 的模型上不报错
3. 脑区注入正确（新实体归入正确脑区）
4. 查询功能正常（hybrid/local/naive 模式）
5. 流式查询正常（stream=True 返回 AsyncIterator）
6. MCP Sampling 回调仍正常工作（`call_llm_via_litellm` 和 `get_llm_config` 未被破坏）
7. 主 Agent 对话不受影响
