# LightRAG LLM 调用统一走 SDK — 设计文档

**Goal:** 将 LightRAG 的 LLM 调用从 OpenAI SDK 直连改为直接调 LiteLLM，消除模型兼容性问题，同时将脑区注入从代理层拦截改为 LightRAG 提示词模板层。

## 问题根因

LightRAG 的 `openai_complete_if_cache` 使用 OpenAI SDK 专有的 `chat.completions.parse()` + Pydantic 类做 `response_format`（Structured Outputs 功能），这在非 OpenAI 模型上报错。我们的 Agent 从来没这个问题，因为我们用 LiteLLM，它自动处理各厂商差异。

## 设计原则

1. **应用层只管按 SDK 标准写请求**，模型兼容性由 LiteLLM 处理
2. **代理程序不改**，它已经在正常工作
3. **脑区注入搬到 LightRAG 模板层**，不再靠 HTTP 代理层拦截
4. **只改需要改的地方**，不重构、不扩展、不加新模块

## 架构变更

### 改造前

```
LightRAG operate.py
  → llm_model_func(prompt, keyword_extraction=True, ...)
    → openai_complete_if_cache("proxy-model", prompt, base_url=PROXY_BASE_URL, ...)
      → OpenAI SDK chat.completions.parse()  ← 问题根源：非 OpenAI 模型不支持
        → HTTP POST to localhost:9876
          → llm_proxy.py chat_completions()
            → inject_brain_region_context()  ← 脑区注入在代理层
            → call_llm_via_litellm()
              → LiteLLMSession.chat()  ← 这才是正确的调用方式
```

### 改造后

```
LightRAG operate.py
  → llm_model_func(prompt, keyword_extraction=True, ...)
    → _llm_model_func 内部：
      1. 处理 keyword_extraction：转成标准 response_format 字典
      2. 构建 LiteLLMSession 配置
      3. 调用 LiteLLMSession.chat()  ← 直接调 SDK，不经过 HTTP
      4. 返回 str 或 AsyncIterator[str]
```

脑区注入路径：

```
LightRAG operate.py
  → entity_extraction_system_prompt.format(brain_region_list=动态脑区列表)
    → _llm_model_func 收到的 system_prompt 已包含脑区信息
```

## 具体改动

### 改动 1: `_llm_model_func` — 从 OpenAI SDK 改为直接调 LiteLLM

**文件**: `niu_api/internal/lightrag_manager.py`

核心逻辑：
1. 不再 import `openai_complete_if_cache`
2. 用 `LiteLLMSession` 直接调 `litellm.completion()`
3. `keyword_extraction=True` 时，把 `GPTKeywordExtractionFormat` 转成标准 `response_format` 字典传给 LiteLLM
4. `stream=True` 时返回 `AsyncIterator[str]`
5. 配置从 `get_llm_config(use_lightrag_config=True)` 获取（跟代理用同样的配置源）

关于 `drop_params`：当传 `response_format` 时应启用 `drop_params=True`，这样 LiteLLM 在模型不支持时自动丢弃该参数而不是抛异常。

关于 `hashing_kv`：LightRAG 通过 `partial()` 把 `hashing_kv` 注入到 `llm_model_func` 的 kwargs 中，`_llm_model_func` 内部需要从 kwargs 弹出它，不做缓存（缓存由 LightRAG 上层的 `use_llm_func_with_cache` 管理）。

### 改动 2: 脑区注入 — 从代理层拦截改为模板层写入

**文件 1**: `REDACTED_USER_PATH/tools/LightRAG/lightrag/prompt.py`

在 `entity_extraction_system_prompt` 模板末尾追加：
- 静态规则（当前 `_STATIC_BRAIN_REGION_PROMPT` 的内容，直接写入模板文本）
- `{brain_region_list}` 占位符（动态脑区列表）

**文件 2**: `REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py`

在 `context_base` 字典构建时（约第 2953 行），增加 `brain_region_list` 的填充逻辑：
- 从 LightRAG 实例的 NetworkX 内存图读取 `entity_type == "brainregion"` 的节点名
- 格式化为 `"脑区名1、脑区名2、..."` 字符串

**文件 3**: `niu_api/llm_proxy.py`

移除 `inject_brain_region_context` 的调用（第 408-416 行），不再需要代理层拦截注入。

**文件 4**: `niu_api/internal/brain_region_prompt.py`

此文件仍保留（`build_dynamic_brain_region_prompt` 等函数可能被其他地方引用），但 `inject_brain_region_context` 不再被 `llm_proxy.py` 调用。

### 改动 3: 清理不再需要的代码

**文件**: `niu_api/internal/lightrag_manager.py`

- 删除 `from lightrag.llm.openai import openai_complete_if_cache`
- 删除 `_get_shared_openai_client` 函数及其全局变量 `_shared_openai_client`、`_client_lock`
- 删除 `PROXY_BASE_URL` 常量（如果 `get_lightrag_status` 不再需要它）——需要检查

## 不改的东西

1. **`niu_api/llm_proxy.py`** — 不改核心逻辑，只删掉脑区注入拦截
2. **`agent/generic/litellm_adapter.py`** — 不改，`LiteLLMSession` 原样使用
3. **LightRAG 的 `operate.py`** — 只加一行 `brain_region_list` 填充，不重构
4. **嵌入模型** — 不改，已经是本地直接调用
5. **`response_format_handler.py`** — 不创建，LiteLLM 自带降级能力
6. **探测/缓存/prompt注入** — 不做，LiteLLM 自动处理

## 验证标准

1. LightRAG 实体提取正常工作（新文档入库成功）
2. `keyword_extraction` 在不支持 `response_format` 的模型上不报错
3. 脑区注入正确（新实体归入正确脑区）
4. 查询功能正常（hybrid/local/naive 模式）
5. 流式查询正常（stream=True 返回 AsyncIterator）
