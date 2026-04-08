# Agent Protocol 重构设计

> 日期：2026-04-08
> 状态：草稿

---

## 背景问题

### 现状：文本协议层是多余的

当前代码的数据流：

```
agent_runner_loop(client, messages, tools_schema)
  └→ ToolClient.chat(messages, tools)
       └→ _build_protocol_prompt(messages, tools)   # 把 messages 转成字符串
            "系统提示\n### 交互协议\n可用工具库: [...]\n=== USER ===\n..."
       └→ backend.ask(full_prompt_text, tools=tools)
            └→ LiteLLMSession.raw_ask(full_prompt_text)
                 └→ litellm.completion(model, messages=[full_prompt_text], ...)
```

**问题1：LiteLLM 被当成了文本补全 API 用**
LiteLLM 的核心能力是原生 tool calling：`litellm.completion(model, messages=[{role, content}], tools=[...])`
当前代码把整个对话转成一个大字符串塞进 `messages[0].content`，LiteLLM 内部无法区分 system/user/assistant 消息，tool calling 机制被削弱。

**问题2：`_build_protocol_prompt()` 的文本协议对原生 tool calling 模型有害**
MiniMax 在有文本协议时：`stop_reason=end_turn`（不调用工具）
MiniMax 无文本协议时：`stop_reason=tool_use`（正常调用工具）

**问题3：thinking 链占用 token 但没有任何功能性作用**
- `response.thinking` 从未被任何 handler 读取用于决策
- thinking 内容只写到日志（调试用），用户从来看不到
- 对于原生支持 thinking 的模型（MiniMax/DeepSeek），SDK 已经处理，不需要文本指令

### 设计原则

1. **让 SDK 做它该做的事**：LiteLLM 负责处理不同模型的 tool calling 差异，应用层不要叠加自定义文本协议
2. **裸传 messages + tools**：应用层只负责把原始对话消息和工具定义传给 LiteLLM，不做转换
3. **不需要文本协议**：无论模型是否原生支持 tool calling，都不通过文本指令来调用工具

---

## 架构设计

### 当前架构

```
agent_runner_loop
  → ToolClient.chat(messages, tools)
       → _build_protocol_prompt()  ← 问题所在
       → backend.ask(string_prompt, tools=tools)
       → LiteLLMSession.raw_ask(string_prompt)
       → litellm.completion(messages=[string_prompt])  ← 错的
```

### 重构后架构

```
agent_runner_loop
  → ToolClient.chat(messages, tools)
       → LiteLLMSession.chat(messages, tools)  ← 直接传递，不转字符串
       → litellm.completion(model, messages=[...], tools=[...])  ← 原生调用
```

LiteLLM 内部根据模型类型（MiniMax/OpenAI/Claude/DeepSeek 等）自动选择正确的 tool calling 方式：
- MiniMax → Anthropic 格式 `tools` 参数
- OpenAI → `tools` 参数
- Claude → `tools` 参数
- DeepSeek → `reasoning` + `tools` 参数

应用层不需要知道这些差异。

---

## 改动详情

### 1. `LiteLLMSession` 增加 `chat()` 方法

在 `litellm_adapter.py` 的 `LiteLLMSession` 类中增加：

```python
def chat(self, messages: List[Dict], tools: Optional[List] = None, **kwargs):
    """
    原生 LiteLLM 调用。

    直接将 messages + tools 传给 LiteLLM，不经过文本协议层。
    LiteLLM 根据模型类型选择正确的 API 格式。

    Returns:
        MockResponse with tool_calls extracted from LiteLLM response
    """
    litellm_model = get_litellm_model_name(self.default_model)
    provider_params = get_provider_params(self.default_model)

    litellm_tools = _convert_tools_schema(tools)

    request_params = {
        "model": litellm_model,
        "messages": messages,
        "stream": True,
        **provider_params,
    }
    if litellm_tools:
        request_params["tools"] = litellm_tools

    response = litellm.completion(**request_params)
    return self._parse_stream_response(response)
```

**说明**：不需要 `api_base` 参数（由 `configure_api_base()` 在创建时全局设置）。

### 2. `ToolClient.chat()` 简化为直接转发

删除 `llmcore.py` 中 `ToolClient.chat()` 的协议文本构建逻辑，改为直接调用 backend 的 `chat()` 方法：

```python
def chat(self, messages, tools=None):
    gen = self.backend.chat(messages, tools=tools, stream=True)
    for chunk in gen:
        yield chunk
```

**删除**：
- `_build_protocol_prompt()` 调用
- `_parse_mixed_response()` 调用
- `full_prompt` 日志写入（不再有意义）
- `last_tools` 跟踪（不再需要文本工具字符串）

**保留**：
- `last_tools` 字段（`agent_loop.py` 每 10 轮重置一次，不影响功能）
- token 统计相关字段

### 3. 删除 `_build_protocol_prompt()` 和 `_prepare_tool_instruction()`

这两个方法是为文本协议设计的，LiteLLM 原生调用不需要它们。

### 4. `BaseSession.ask()` 标记为废弃

`BaseSession.ask()` 方法不再被 `ToolClient.chat()` 调用（因为所有调用方都用 `client.chat()` → `backend.chat()`），但保留其实现避免破坏导入链：

```python
# 已废弃：请使用 chat() 方法代替
def ask(self, prompt, model=None, stream=False, tools=None):
    ...
```

**不需要调用方修改**：`runner.py` → `agent_runner_loop()` → `client.chat()` 的调用链不变。

### 5. `LiteLLMSession._parse_stream_response()` 新增

从 `raw_ask()` 的流式解析逻辑提取，解析 LiteLLM 返回的流式响应中的 tool_calls：

```python
def _parse_stream_response(self, response):
    """解析 LiteLLM 流式响应，提取 tool_calls"""
    tool_calls = []
    for chunk in response:
        delta = getattr(chunk, 'choices', [None])[0].delta if hasattr(chunk, 'choices') else None
        if not delta:
            continue
        if hasattr(delta, 'tool_calls') and delta.tool_calls:
            for tc in delta.tool_calls:
                # 提取 tool call
    return MockResponse(thinking="", content="", tool_calls=tool_calls, raw="")
```

（如果 LiteLLM 支持非流式响应，也可以用同步方式简化。）

### 6. thinking 链的处理

**删除**：所有文本协议中的 thinking 链指令文本。

**原因**：
- `response.thinking` 从未被任何 handler 读取用于决策，只写到日志
- thinking 标签只用于从显示内容中 strip 掉，用户实际看不到
- 要求模型输出 thinking 标签不会提升推理质量（模型不是「因为被要求 thinking 才思考」）
- LiteLLM SDK 对支持 native thinking 的模型（MiniMax、DeepSeek）通过 API 参数启用，不需要文本指令

**具体删除**：`_prepare_tool_instruction()` 中的 `1. **思考**: 在 <thinking> 标签中先进行思考...` 整段指令。

**保留**：`handler.py` 中 strip `<thinking>` 标签的正则逻辑（`<thinking>...</thinking>` 可能在模型原生响应中出现，用于 clean up 显示内容）。

---

## 兼容性考虑

### 子 Agent (`subagent.py`)

`subagent.py` 使用 `create_client()` → `ToolClient(LiteLLMSession)`，然后调用 `agent_runner_loop(client, ...)` → `client.chat(messages, tools)`。

重构后路径不变，只是内部不再做文本协议转换。

### 向后兼容

`BaseSession.ask()` 方法保留（但不推荐使用）：
- 旧代码如果直接调用 `session.ask(prompt_string)` 仍能工作（走 LiteLLM 的文本补全）
- 新代码统一使用 `session.chat(messages, tools)`

---

## 数据流对比

### 重构前
```
User: 处理照片DSC_3314.jpg
↓
_build_protocol_prompt():
  system: 你是一个助手，必须使用工具。
  protocol: ### 交互协议...
  tools: [{"name": "chat-with-file-processor", ...}]
  === USER ===
  处理照片DSC_3314.jpg
  === ASSISTANT ===
↓
full_prompt = "你是一个助手...\n### 交互协议..."  (字符串)
↓
litellm.completion(model, messages=[{role: "user", content: full_prompt_string}])
↓
LiteLLM 内部：把字符串当纯文本补全处理，tool calling 能力被削弱
```

### 重构后
```
messages = [
  {"role": "system", "content": "你是一个助手，必须使用工具。"},
  {"role": "user", "content": "处理照片DSC_3314.jpg"}
]
tools = [{"type": "function", "function": {"name": "chat-with-file-processor", ...}}]
↓
litellm.completion(model, messages=messages, tools=tools)
↓
LiteLLM 内部：根据模型选择正确格式
  - MiniMax → Anthropic格式
  - OpenAI → tools参数
  - Claude → tools参数
```

---

## 验证方案

1. **单元测试**：`test_final_verification.py` 验证 MiniMax tool calling（已有）
2. **回归测试**：所有现有 `tests/` 测试通过
3. **手动测试**：拖入照片，验证子 Agent 被正确调用

---

## 改动文件清单

| 文件 | 改动 |
|------|------|
| `agent/generic/litellm_adapter.py` | `LiteLLMSession` 增加 `chat()` 方法，新增 `_parse_stream_response()` |
| `agent/generic/llmcore.py` | `ToolClient.chat()` 简化为直接转发，删除 `_build_protocol_prompt()`、`_prepare_tool_instruction()`、`_parse_mixed_response()` |
| `agent/generic/__init__.py` | 可能需要调整导出（取决于最终结构） |
