# LightRAG LLM 调用统一走 SDK — 设计文档 v3

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

LightRAG LLM：operate.py -> _llm_model_func
  -> asyncio.to_thread(同步调用LiteLLMSession)  <-- 避免阻塞事件循环
  -> LiteLLMSession.chat() -> litellm.completion()  OK 直接SDK

LightRAG Embedding：不变  OK

脑区注入：_llm_model_func 内部 -> get_brain_regions() 读内存图 -> 拼入 system_prompt
```

代理程序路由：删除 `/llm/v1/chat/completions` 和 `/llm/v1/embeddings` 端点（无调用方），保留 `get_llm_config` 和 `call_llm_via_litellm` 函数（被 MCP 客户端引用）。

## 设计原则

1. **应用层只管按 SDK 标准写请求**，模型兼容性由 LiteLLM 处理
2. **代理程序函数逻辑不动**，只删没人用的路由端点
3. **脑区注入搬到 `_llm_model_func` 内部**，用已有的 `get_brain_regions()` 读内存图
4. **只改需要改的地方**，最小化改动范围

## 具体改动

### 改动 1: `_llm_model_func` — 从 OpenAI SDK 改为直接调 LiteLLM

**文件**: `niu_api/internal/lightrag_manager.py`

**删除的代码：**
- `from lightrag.llm.openai import openai_complete_if_cache`
- `_get_shared_openai_client` 函数及全局变量 `_shared_openai_client`、`_client_lock`
- `PROXY_BASE_URL` 常量
- `PROXY_API_KEY` 常量

**新增的代码：**
- `from agent.generic.litellm_adapter import LiteLLMSession`
- `from niu_api.llm_proxy import get_llm_config`
- `from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt, build_dynamic_brain_region_prompt`

**新的 `_llm_model_func` 逻辑：**

1. 弹出 LightRAG 内部参数：`hashing_kv`、`_priority`、`_timeout`、`_queue_timeout`（不传给 LiteLLM，这些是 LightRAG 并发控制参数）
2. 检测是否为实体提取请求（`system_prompt` 包含 `"Knowledge Graph Specialist"`）
   - 是：调 `build_static_brain_region_prompt()` + `build_dynamic_brain_region_prompt()`，拼入 `system_prompt` 末尾
   - 幂等保护：如果 system_prompt 已包含 `"大脑区域架构"`，跳过注入（防止双重注入）
3. 构建 messages 列表（`system_prompt` + `history_messages` + `prompt`）
   - history_messages 中 content=None 的消息，content 替换为空字符串 ""（litellm 不保证所有 provider 能处理 content=None）
4. 从 `get_llm_config(use_lightrag_config=True)` 获取 LLM 配置
5. 处理 `keyword_extraction`：构建标准 `response_format` 字典
6. 用 `asyncio.to_thread` 包装同步调用，避免阻塞 LightRAG 事件循环
7. 在同步函数内部：构建 `LiteLLMSession` 实例，调用 `session.chat()`，消费 Generator 获取 `MockResponse`
8. 处理 `enable_cot`：从 `MockResponse.thinking` 提取思考内容，按 `openai_complete_if_cache` 的逻辑包装
9. 根据 LightRAG 传入的 `stream` 参数决定返回格式：
   - **stream=False**（默认）：返回 `str`
   - **stream=True**：返回 `AsyncIterator[str]`。实现方式：消费 Generator 获取完整内容后，将字符串按固定大小分块，用 async generator 逐块 yield。注意：这不是真正的流式（数据已全部到达），但满足 LightRAG 的 `AsyncIterator[str]` 接口要求。对于知识图谱查询场景，流式体验不是刚需。COT 标签会在完整内容前面统一包装，不是逐 chunk 交错输出，与旧方案有行为差异，但不影响功能（LightRAG 的 `remove_think_tags` 会清理所有思考标签）

**关键细节：**

- **异步/同步桥接**：`_llm_model_func` 是 async 函数，`LiteLLMSession.chat()` 是同步 Generator。必须用 `asyncio.to_thread` 包装整个同步调用过程（构建 session -> 调用 chat() -> 消费 Generator），与 `call_llm_via_litellm` 的做法一致
- **LiteLLMSession.chat() 不接受 stream 参数**：内部硬编码 `stream=True`，始终返回 `Generator[str, None, MockResponse]`。不管 LightRAG 传不传 stream，消费方式一样。消费完后根据 LightRAG 传入的 stream 参数决定返回格式
- **配置获取与键名映射**：`get_llm_config(use_lightrag_config=True)` 返回的键名是 `type`，但 `LiteLLMSession` 构造函数读取的是 `api_type`。必须做映射：`"api_type": config.get("type", "openai")`，与 `call_llm_via_litellm`（llm_proxy.py 第278行）的做法一致。完整配置构建：
  ```python
  llm_config = {
      "api_type": config.get("type", "openai"),  # type -> api_type 映射
      "apikey": config["apikey"],
      "apibase": config["apibase"],
      "model": config["model"],
      "reasoning_effort": config.get("reasoning_effort"),
  }
  ```
- **keyword_extraction 的 response_format 构建**：`GPTKeywordExtractionFormat.model_json_schema()` 返回裸 JSON Schema，litellm 期望的格式是 `{"type": "json_schema", "json_schema": {"name": ..., "strict": True, "schema": {...}}}`，需要手动包装。当模型不支持时，`drop_params=True` 会使 LiteLLM 静默丢弃 `response_format`，退化为普通文本生成，LightRAG 的 `json_repair.loads()` 仍能解析
- **drop_params**：传 `response_format` 时必须设 `drop_params=True`。由于 `LiteLLMSession.chat()` 内部只在 `reasoning_effort` 存在时才设 `drop_params`，需要改动 `litellm_adapter.py`（见改动 2）
- **脑区注入**：复用 `brain_region_prompt.py` 中的 `build_static_brain_region_prompt()` 和 `build_dynamic_brain_region_prompt()`（后者内部调 `get_brain_regions()` 读内存图），拼接到 `system_prompt` 末尾。`get_brain_regions()` 是纯同步读内存，不进 asyncio 事件循环，不会死锁
- **enable_cot 处理**：与 `openai_complete_if_cache` 行为一致——非流式时，`MockResponse.thinking` 非空且 `MockResponse.content` 也非空则忽略 thinking 只返回 content；thinking 非空但 content 为空则返回思考标签包裹的 thinking。流式时，由于 `LiteLLMSession.chat()` 的 Generator 不 yield thinking 内容（只 yield delta.content），thinking 只在 `MockResponse` 中获取，所以流式场景下 COT 标签在消费完 Generator 后统一包装到内容前面
- **LiteLLMSession 实例缓存**：缓存一个共享的 `LiteLLMSession` 实例，用配置元组 `(model, api_base, api_key, api_type)` 作为缓存 key。配置变化时（用户改配置文件）自动重建。避免实体提取高频调用时每次新建实例的连接初始化开销
- **get_lightrag_status 的 proxy_base_url 字段**：直接删除该字段。改造后不再有代理，`proxy_base_url` 的语义已不存在。前端不使用该字段（已验证 ui/ 目录无引用）

### 改动 2: `litellm_adapter.py` — 传 response_format 时设 drop_params

**文件**: `agent/generic/litellm_adapter.py`

在 `LiteLLMSession.chat()` 方法中，增加一行：

```python
if response_format is not None:
    request_params["drop_params"] = True
```

放在已有的 `if provider_params.get("reasoning_effort"): request_params["drop_params"] = True` 旁边。这样传 `response_format` 时，不支持该参数的模型会静默丢弃而不是抛异常。

### 改动 3: 删除代理程序中没人用的路由端点

**文件**: `niu_api/llm_proxy.py`

- 删除 `chat_completions` 端点函数
- 删除 `/llm/v1/embeddings` 端点函数
- 删除第27行的 `from niu_api.internal.brain_region_prompt import inject_brain_region_context, is_lightrag_extraction_request` 整条 import 语句
- 删除脑区注入拦截代码（`inject_brain_region_context` 调用，约第408-416行）
- 删除 `is_lightrag` 判断相关代码
- 保留 `get_llm_config`、`call_llm_via_litellm` 函数（被 MCP 客户端引用）
- 保留 `openai_to_litellm_messages`、`openai_to_litellm_tools`、`litellm_to_openai_response` 等辅助函数及 Pydantic 模型（虽当前仅被已删端点引用，但保留以备扩展，避免破坏性变更）
- 保留 `/llm/v1/models`、`/llm/v1/health`、`/llm/v1/status` 等辅助端点
- 保留路由注册（router 对象仍需存在，否则 `__main__.py` 的 import 会报错）
- 更新模块 docstring（第1-17行）：移除对已删端点的描述，改为描述剩余端点

**文件**: `niu_api/__main__.py`

- 检查路由注册是否需要调整

### 改动 4: 清理不再需要的代码

**文件**: `niu_api/internal/lightrag_manager.py`

- 删除 `PROXY_BASE_URL` 和 `PROXY_API_KEY` 常量
- 删除 `_get_shared_openai_client` 函数及全局变量
- `get_lightrag_status` 函数中直接删除 `proxy_base_url` 字段
- 更新模块 docstring 中的架构描述：从 `routed through /llm/v1/ proxy` 改为 `direct LiteLLMSession.chat()`

**文件**: `niu_api/internal/brain_region_prompt.py`

- `inject_brain_region_context` 函数不再被 `llm_proxy.py` 调用，但保留（`build_static_brain_region_prompt` 和 `build_dynamic_brain_region_prompt` 被 `_llm_model_func` 使用）

### 改动 5: 更新测试文件

- `tests/test_llm_proxy.py` — 删除对 `chat_completions` 和 `embeddings` 端点的测试
- `tests/test_llm_proxy_injection.py` — 删除对代理层脑区注入的测试（注入已搬到 `_llm_model_func`）
- `tests/test_lightrag_manager.py` — 删除 `proxy_base_url` 字段的断言

## 不改的东西

1. **LightRAG fork 的 `operate.py`、`prompt.py`** — 不改，脑区注入在 `_llm_model_func` 层做
2. **LightRAG Embedding** — 不改，已经走本地模型
3. **`call_llm_via_litellm` 函数** — 保留，被 MCP 客户端的 Sampling 回调引用
4. **`get_llm_config` 函数** — 保留，被 MCP 客户端和新的 `_llm_model_func` 引用
5. **`scripts/benchmark_lightrag_ingest.py`** — 已知影响（proxy 模式失效），不在本次改动范围

## 涉及的文件清单

| 文件 | 操作 |
|------|------|
| `niu_api/internal/lightrag_manager.py` | 改造 `_llm_model_func`，删除旧代码 |
| `agent/generic/litellm_adapter.py` | 传 response_format 时设 drop_params=True |
| `niu_api/llm_proxy.py` | 删除 chat_completions 和 embeddings 端点，删除脑区拦截 |
| `niu_api/__main__.py` | 检查路由注册 |
| `niu_api/internal/brain_region_prompt.py` | 不改，保留供 `_llm_model_func` 调用 |
| `tests/test_llm_proxy.py` | 删除对已删端点的测试 |
| `tests/test_llm_proxy_injection.py` | 删除对代理层脑区注入的测试 |
| `tests/test_lightrag_manager.py` | 更新 proxy_base_url 断言 |

## 验证标准

1. LightRAG 实体提取正常工作（新文档入库成功）
2. `keyword_extraction` 在不支持 `response_format` 的模型上不报错
3. 脑区注入正确（新实体归入正确脑区）
4. 查询功能正常（hybrid/local/naive 模式）
5. 流式查询正常（stream=True 返回 AsyncIterator）
6. MCP Sampling 回调仍正常工作（`call_llm_via_litellm` 和 `get_llm_config` 未被破坏）
7. 主 Agent 对话不受影响
8. 不存在双重脑区注入（幂等保护生效）
