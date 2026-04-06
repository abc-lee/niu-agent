# 上下文管理混乱问题分析报告

## 执行摘要

**核心问题**：项目中存在 **三个独立的上下文存储**，它们之间没有正确同步，导致 `/new` 清空对话后，LLM 仍然收到历史记录。

**三个存储层次**：
1. **SQLite 数据库**（`MessageStore`）- 持久化存储
2. **LLM Session 内存缓存**（`BaseSession.history`）- GenericAgent 内部
3. **发送给 LLM 的 Prompt**（`messages` 参数）- 每次请求

---

## 1. 完整的上下文管理流程

### 1.1 用户发送消息的流程

```
用户输入
    ↓
前端 chat.html
    ↓ IPC: send-message
main.js
    ↓ HTTP POST: /api/chat/session
compat.py: chat_session()
    ↓
    ├─ MessageStore.add_message(role="user", content=message)  [存入 SQLite]
    ├─ MessageStore.get_messages(limit=100)                    [从 SQLite 加载历史]
    ↓
    history = [{"role": "user/assistant", "content": ...}, ...]  [转换格式]
    ↓
runner.chat(session_id, message, history=history)
    ↓
agent_loop.py: agent_runner_loop(messages=messages, history=history)
    ↓
    ├─ messages = [system_prompt] + history + [当前用户消息]  [构建完整 messages]
    ↓
client.chat(messages=messages, tools=tools_schema)
    ↓
ToolClient.chat() 或 NativeToolClient.chat()
    ↓
    ├─ _build_protocol_prompt(messages, tools)  [构建 prompt 字符串]
    │   └─ 提取 history_msgs，转换为 prompt 文本
    │   └─ system + "=== USER ===" + content + "=== ASSISTANT ===" + content
    ↓
backend.ask(prompt, stream=True)  [发送给 LLM API]
    ↓
BaseSession.ask(prompt)
    ├─ self.history.append({"role": "user", "content": [{"type": "text", "text": prompt}]})  [追加到内部 history]
    ├─ messages = self.make_messages(self.history)  [使用内部 history，不是传入的 messages！]
    ↓
raw_ask(messages, model)  [真正发送给 LLM API]
    ↓
LLM 响应
    ├─ self.history.append({"role": "assistant", ...})  [追加响应到内部 history]
    ↓
存入 SQLite
    └─ MessageStore.add_message(role="assistant", content=reply)
```

---

### 1.2 `/new` 清空对话的流程

```
用户输入 /new
    ↓
前端 chat.html
    ↓ IPC: clear-chat
main.js
    ↓ HTTP POST: /api/chat/clear
compat.py: clear_chat()
    ├─ MessageStore.clear_messages()               [清空 SQLite]
    ├─ runner.handler.reset_working_memory()       [清空工作记忆]
    ├─ runner.client.backend.history = []          [清空 LLM Session 内存缓存]
    └─ return {"success": True}
```

---

## 2. 问题根源分析

### 2.1 **关键 Bug**：ToolClient.chat() 忽略了传入的 messages 参数

**位置**：`agent/generic/llmcore.py` 第 970-992 行

```python
class ToolClient:
    def chat(self, messages, tools=None):
        # ❌ 问题：直接使用 backend.history，忽略了传入的 messages
        full_prompt = self._build_protocol_prompt(messages, tools)
        gen = self.backend.ask(full_prompt, stream=True)
        ...
```

**BaseSession.ask() 的实现**（第 658-690 行）：

```python
class BaseSession:
    def ask(self, prompt, model=None, stream=False):
        def _ask_gen():
            content = ""
            with self.lock:
                # ❌ 问题：每次都追加到 self.history
                self.history.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
                trim_messages_history(self.history, self.context_win)
                # ❌ 问题：使用 self.history，不是传入的 messages
                messages = self.make_messages(self.history)

            # 发送给 LLM API
            content_blocks = None
            gen = self.raw_ask(messages, model)
            ...
```

**结果**：
- `ToolClient.chat()` 将传入的 `messages` 转换为 prompt 字符串
- `BaseSession.ask()` 接收 prompt 字符串后，**又追加到 `self.history`**
- `BaseSession.ask()` 使用 `self.history` 构建 messages，**忽略了传入的内容**

---

### 2.2 **数据流断裂**：三个存储不同步

| 存储位置 | 清空时是否清空 | 发送消息时是否追加 | 数据来源 |
|---------|--------------|------------------|---------|
| **SQLite** (`MessageStore`) | ✅ 清空 | ✅ 追加 | 从数据库加载 |
| **LLM Session** (`backend.history`) | ✅ 清空 | ✅ 追加 | 每次调用 `ask()` 时追加 |
| **Prompt messages** | ❌ 不清空（每次重新构建） | N/A | 从 SQLite 加载 + Session.history |

**问题场景**：

1. **用户发送消息 A**
   - SQLite: `[A]`
   - backend.history: `[prompt(A)]` (包含 system prompt)
   - LLM 收到: `system + prompt(A)`

2. **用户发送消息 B**
   - SQLite: `[A, B]`
   - backend.history: `[prompt(A), prompt(B)]` ⚠️ **注意：A 的 prompt 被重复追加**
   - LLM 收到: `system + (SQLite 的 A 和 B 转换的 prompt)`

3. **用户执行 `/new`**
   - SQLite: `[]` ✅ 已清空
   - backend.history: `[]` ✅ 已清空
   - **但是**：下次发送消息时，会重新从 SQLite 加载历史

4. **用户发送消息 C**
   - SQLite: `[C]` ✅ 正确
   - backend.history: `[prompt(C)]` ✅ 正确
   - LLM 收到: `system + prompt(C)` ✅ 正确

**结论**：理论上应该正确，但是...

---

### 2.3 **真正的 Bug**：NativeToolClient 使用不同的 Session 类型

**位置**：`agent/runner.py` 第 156-178 行

```python
def create_client(config: Dict[str, Any]):
    """创建 LLM 客户端"""
    client_type = config.get("type", "openai")

    if client_type in ("native_claude", "native"):
        session = NativeClaudeSession(cfg)
        return NativeToolClient(session)  # ❌ 返回 NativeToolClient
    elif client_type in ("native_openai", "native_oai"):
        session = NativeOAISession(cfg)
        return NativeToolClient(session)  # ❌ 返回 NativeToolClient
    elif client_type in ("claude", "anthropic"):
        session = ClaudeSession(cfg)
        return ToolClient(session)
    else:  # openai or default
        session = LLMSession(cfg)
        return ToolClient(session)
```

**NativeClaudeSession.ask() 的实现**（第 837-888 行）：

```python
class NativeClaudeSession(BaseSession):
    def ask(self, msg, tools=None, model=None):
        assert type(msg) is dict
        with self.lock:
            # ❌ 问题：直接追加到 self.history
            self.history.append(msg)
            trim_messages_history(self.history, self.context_win)
            messages = list(self.history)  # ❌ 使用 self.history

        content_blocks = None
        gen = self.raw_ask(messages, tools, self.system, model)
        ...
```

**关键差异**：
- `NativeClaudeSession.ask(msg)` 接收的参数是 **dict**（完整的消息对象）
- `BaseSession.ask(prompt)` 接收的参数是 **str**（prompt 字符串）
- **两者都使用 `self.history`**，不是传入的 messages

**NativeToolClient.chat() 的实现**（第 1387-1449 行）：

```python
class NativeToolClient:
    def chat(self, messages, tools=None):
        # ...
        for msg in messages:
            c = msg.get("content", "")
            if msg["role"] == "system":
                self.set_system(c)
                continue
            if isinstance(c, str):
                combined_content.append({"type": "text", "text": c})
            elif isinstance(c, list):
                combined_content.extend(c)
            # ❌ 问题：提取所有 messages 的内容，合并成一个 user 消息
            if msg["role"] == "user" and msg.get("tool_results"):
                tool_results.extend(msg["tool_results"])

        merged = {"role": "user", "content": tool_result_blocks + combined_content}
        gen = self.backend.ask(merged, self.tools)  # ❌ 传给 NativeClaudeSession
        ...
```

**问题**：
- `NativeToolClient.chat()` 接收 `messages` 参数（包含完整历史）
- 它将 `messages` 合并成一个 `merged` 消息
- 传给 `NativeClaudeSession.ask(merged)`
- `NativeClaudeSession.ask()` 将 `merged` 追加到 `self.history`
- **结果：历史被重复追加！**

---

## 3. 具体问题演示

### 场景 1：使用 NativeClaudeSession（native_claude 类型）

**初始状态**：
- SQLite: `[]`
- backend.history: `[]`

**用户发送消息 A**：
1. `compat.py` 从 SQLite 加载历史：`history = []`
2. `runner.chat(history=[])`
3. `agent_loop.py` 构建 messages: `[system, {"role": "user", "content": "A"}]`
4. `NativeToolClient.chat(messages)` 提取内容，构建 `merged = {"role": "user", "content": "system+A"}`
5. `NativeClaudeSession.ask(merged)` 追加到 history
   - backend.history: `[{"role": "user", "content": "system+A"}]`
6. `NativeClaudeSession.ask()` 发送给 LLM：`messages = [system, user("A")]`
7. LLM 响应 "reply_A"
8. `NativeClaudeSession.ask()` 追加响应
   - backend.history: `[user("system+A"), assistant("reply_A")]`
9. 存入 SQLite: `[A, reply_A]`

**用户发送消息 B**：
1. `compat.py` 从 SQLite 加载历史：`history = [A, reply_A]`
2. `runner.chat(history=[A, reply_A])`
3. `agent_loop.py` 构建 messages: `[system, A, reply_A, {"role": "user", "content": "B"}]`
4. `NativeToolClient.chat(messages)` 提取内容，构建 `merged = {"role": "user", "content": "system+A+reply_A+B"}`
5. `NativeClaudeSession.ask(merged)` 追加到 history
   - backend.history: `[user("system+A"), assistant("reply_A"), user("system+A+reply_A+B")]` ⚠️ **重复！**
6. `NativeClaudeSession.ask()` 发送给 LLM：`messages = [user("system+A"), assistant("reply_A"), user("system+A+reply_A+B")]`
7. **LLM 收到重复的历史！**

**用户执行 `/new`**：
1. 清空 SQLite: `[]`
2. 清空 backend.history: `[]`
3. ✅ 下次发送消息时，从空历史开始

---

### 场景 2：使用 LLMSession（openai 类型）

**初始状态**：
- SQLite: `[]`
- backend.history: `[]`

**用户发送消息 A**：
1. `compat.py` 从 SQLite 加载历史：`history = []`
2. `runner.chat(history=[])`
3. `agent_loop.py` 构建 messages: `[system, {"role": "user", "content": "A"}]`
4. `ToolClient.chat(messages)` 调用 `_build_protocol_prompt(messages)`
   - 提取 system + history_msgs
   - 构建 prompt: `system + "=== USER ===\nA\n=== ASSISTANT ===\n"`
5. `BaseSession.ask(prompt)` 追加到 history
   - backend.history: `[{"role": "user", "content": [{"type": "text", "text": "system+=== USER ===\nA\n=== ASSISTANT ===\n"}]}]`
6. `BaseSession.ask()` 发送给 LLM：`messages = backend.history`
7. LLM 响应 "reply_A"
8. `BaseSession.ask()` 追加响应
   - backend.history: `[user("system+..."), assistant("reply_A")]`
9. 存入 SQLite: `[A, reply_A]`

**用户发送消息 B**：
1. `compat.py` 从 SQLite 加载历史：`history = [A, reply_A]`
2. `runner.chat(history=[A, reply_A])`
3. `agent_loop.py` 构建 messages: `[system, A, reply_A, {"role": "user", "content": "B"}]`
4. `ToolClient.chat(messages)` 调用 `_build_protocol_prompt(messages)`
   - 构建 prompt: `system + "=== USER ===\nA\n=== ASSISTANT ===\nreply_A\n=== USER ===\nB\n=== ASSISTANT ===\n"`
5. `BaseSession.ask(prompt)` 追加到 history
   - backend.history: `[user("system+...A..."), assistant("reply_A"), user("system+...A+reply_A+B...")]` ⚠️ **重复！**
6. `BaseSession.ask()` 发送给 LLM：`messages = backend.history`
7. **LLM 收到重复的历史！**

---

## 4. 问题总结

### 4.1 核心问题

**所有 Session 类型都存在"双重历史"问题**：
1. 外部传入的 `messages` 参数（包含从 SQLite 加载的历史）
2. Session 内部的 `self.history`（自己维护的历史）

**Session 忽略了传入的 messages，只使用自己的 history**！

### 4.2 为什么清空后还有历史？

**可能的场景**：
1. **清空不完整**：只清空了 SQLite，没有清空 `backend.history`
   - ✅ 已修复：`compat.py` 第 272-275 行已清空

2. **清空顺序问题**：清空 SQLite 和 backend.history 之间，有新的消息被追加
   - ❌ 不太可能：`clear_chat()` 是同步操作

3. **多个 runner 实例**：如果存在多个 runner，清空了一个，另一个还有历史
   - ❌ 不太可能：`get_runner()` 返回全局单例

4. **缓存未清除**：前端或后端有缓存
   - ❌ 需要检查：前端是否缓存了消息

5. **真正的问题**：**Session 的 history 每次调用都会追加传入的内容，导致重复**
   - ✅ 这才是根本原因！

---

## 5. 修复建议

### 5.1 方案 1：让 Session 使用传入的 messages（推荐）

**修改位置**：`agent/generic/llmcore.py`

**修改 BaseSession.ask()**：

```python
class BaseSession:
    def ask(self, prompt, model=None, stream=False):
        def _ask_gen():
            content = ""
            with self.lock:
                # ❌ 删除：不再追加到 self.history
                # self.history.append({"role": "user", "content": [{"type": "text", "text": prompt}]})

                # ✅ 修改：直接使用传入的 prompt 构建 messages
                # 但是 BaseSession.ask() 只接收 prompt 字符串，无法获取完整的 messages
                # 需要修改接口...

                # 临时方案：不使用 self.history，直接发送 prompt
                messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

            trim_messages_history(messages, self.context_win)  # ⚠️ 需要修改 trim_messages_history

            content_blocks = None
            gen = self.raw_ask(messages, model)
            ...
```

**问题**：`BaseSession.ask()` 只接收 `prompt` 字符串，无法传入完整的 messages。需要修改接口。

---

### 5.2 方案 2：让 ToolClient.chat() 直接调用 raw_ask（推荐）

**修改位置**：`agent/generic/llmcore.py` 第 970-992 行

```python
class ToolClient:
    def chat(self, messages, tools=None):
        # ✅ 直接使用传入的 messages，不经过 backend.ask()
        # 提取 system prompt
        system_from_messages = next((m["content"] for m in messages if m["role"].lower() == "system"), "")
        if system_from_messages:
            self._system_prompt = system_from_messages
        system_content = self._system_prompt

        history_msgs = [m for m in messages if m["role"].lower() != "system"]
        tool_instruction = self._prepare_tool_instruction(tools)

        # 构建 prompt
        user = ""
        for m in history_msgs:
            role = "USER" if m["role"] == "user" else "ASSISTANT"
            user += f"=== {role} ===\n"
            for tr in m.get("tool_results", []):
                user += f"<tool_result>{tr['content']}</tool_result>\n"
            user += str(m["content"]) + "\n"
        user += "=== ASSISTANT ===\n"

        full_prompt = system_content + tool_instruction + user
        print("[Debug] Full prompt length:", len(full_prompt), "chars", file=sys.stderr, flush=True)

        # ✅ 直接调用 backend.raw_ask()，不使用 backend.ask()
        # backend.raw_ask() 不会修改 backend.history
        messages_for_llm = [{"role": "user", "content": [{"type": "text", "text": full_prompt}]}]
        gen = self.backend.raw_ask(messages_for_llm, model=self.backend.default_model)

        raw_text = ""
        summarytag = "[NextWillSummary]"
        for chunk in gen:
            raw_text += chunk
            if chunk != summarytag:
                yield chunk
        if raw_text.endswith(summarytag):
            self.last_tools = ""
            raw_text = raw_text[: -len(summarytag)]

        resp = self._parse_mixed_response(raw_text)
        return resp
```

**优点**：
- 不修改 `BaseSession.ask()` 接口
- 直接使用传入的 messages
- 避免双重历史

**缺点**：
- 绕过了 `BaseSession` 的历史管理
- 需要手动处理缓存等其他功能

---

### 5.3 方案 3：禁用 Session 内部的 history（最简单）

**修改位置**：`agent/generic/llmcore.py` 第 633-657 行

```python
class BaseSession:
    def __init__(self, cfg):
        self.api_key = cfg["apikey"]
        self.api_base = cfg["apibase"].rstrip("/")
        self.default_model = cfg.get("model", "")
        self.context_win = cfg.get("context_win", 24000)
        self.history = []  # ❌ 删除或禁用
        self.lock = threading.Lock()
        self.system = ""
        ...

    def ask(self, prompt, model=None, stream=False):
        def _ask_gen():
            content = ""
            with self.lock:
                # ✅ 不再使用 self.history
                # 直接使用传入的 prompt 构建 messages
                messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

            trim_messages_history(messages, self.context_win)

            content_blocks = None
            gen = self.raw_ask(messages, model)
            ...
```

**优点**：
- 最简单
- 直接禁用内部历史

**缺点**：
- 破坏了 Session 的原有设计
- 可能影响其他使用 Session 的地方

---

### 5.4 方案 4：让 agent_loop 直接传递 messages，不经过 Session 的 history（推荐）

**修改位置**：`agent/generic/agent_loop.py` 第 70-181 行

**当前逻辑**：
- `agent_runner_loop()` 接收 `history` 参数
- 构建 `messages = [system] + history + [当前用户]`
- 调用 `client.chat(messages)`
- `ToolClient.chat()` 使用传入的 `messages` 构建 prompt
- `BaseSession.ask()` 接收 prompt，追加到 `self.history`

**修改逻辑**：
- `agent_runner_loop()` 接收 `history` 参数
- 构建 `messages = [system] + history + [当前用户]`
- 调用 `client.chat(messages, use_internal_history=False)` ✅ 新参数
- `ToolClient.chat()` 如果 `use_internal_history=False`，直接调用 `raw_ask()`

**实现**：

```python
# agent_loop.py
def agent_runner_loop(..., use_internal_history=False):
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        for msg in history:
            ...
    messages.append({"role": "user", "content": initial_user_content})

    response_gen = client.chat(messages=messages, tools=tools_schema, use_internal_history=use_internal_history)
    ...

# llmcore.py
class ToolClient:
    def chat(self, messages, tools=None, use_internal_history=True):
        if not use_internal_history:
            # ✅ 直接使用传入的 messages，不经过 backend.history
            full_prompt = self._build_protocol_prompt(messages, tools)
            messages_for_llm = [{"role": "user", "content": [{"type": "text", "text": full_prompt}]}]
            gen = self.backend.raw_ask(messages_for_llm, model=self.backend.default_model)
            # ... 处理响应
        else:
            # 原有逻辑
            full_prompt = self._build_protocol_prompt(messages, tools)
            gen = self.backend.ask(full_prompt, stream=True)
            ...
```

**优点**：
- 向后兼容
- 显式控制是否使用内部历史

**缺点**：
- 增加了参数，需要修改多处调用

---

## 6. 推荐修复方案

**推荐方案 4**，因为：
1. 向后兼容
2. 显式控制
3. 不破坏现有架构
4. 容易测试和验证

**具体步骤**：

1. 修改 `ToolClient.chat()` 和 `NativeToolClient.chat()`，添加 `use_internal_history` 参数
2. 修改 `agent_runner_loop()`，调用时传递 `use_internal_history=False`
3. 清空时，确保 SQLite 和 `backend.history` 都被清空（已实现）
4. 测试验证

---

## 7. 验证测试

### 测试场景 1：正常对话

```
用户: A
助手: reply_A
用户: B
助手: reply_B
```

**检查**：
- SQLite: [A, reply_A, B, reply_B]
- backend.history: 应该为空或只包含当前请求（如果 use_internal_history=False）

### 测试场景 2：清空对话

```
用户: A
助手: reply_A
用户: /new
用户: B
助手: reply_B
```

**检查**：
- SQLite: [B, reply_B]
- backend.history: []
- LLM 收到的 prompt: 只包含 B

### 测试场景 3：长对话

```
用户: A1...A20 (20 条消息)
助手: reply_A1...reply_A20
用户: B
助手: reply_B
```

**检查**：
- SQLite: 42 条消息
- backend.history: 应该为空或只包含当前请求
- LLM 收到的 prompt: 包含最近 N 条消息（根据 context_win）

---

## 8. 其他发现

### 8.1 前端可能缓存消息

**需要检查**：
- `chat.html` 是否缓存消息
- `preload-chat.js` 是否缓存消息
- `main.js` 是否缓存消息

### 8.2 日志不完整

**需要增强**：
- 在 `ToolClient.chat()` 记录传入的 `messages` 长度
- 在 `BaseSession.ask()` 记录 `self.history` 长度
- 在 `clear_chat()` 记录清空前后的状态

### 8.3 agent_loop 的注释有误导

**位置**：`agent/generic/agent_loop.py` 第 178-180 行

```python
messages = [
    {"role": "user", "content": next_prompt, "tool_results": tool_results}
]  # just new message, history is kept in *Session
```

**误导**：
- 注释说"history is kept in *Session"
- 但实际上，Session 的 history 和传入的 messages 是两套独立的系统
- 导致开发者误以为 Session 会自动管理历史

---

## 9. 总结

**问题根源**：
1. Session 内部维护了 `self.history`
2. 每次 `ask()` 都会追加传入的 prompt
3. `ask()` 使用 `self.history` 而不是传入的 messages
4. 导致历史重复或混乱

**修复方案**：
- 推荐方案 4：添加 `use_internal_history` 参数
- 显式控制是否使用 Session 的内部历史
- 在 `agent_runner_loop` 中禁用内部历史

**验证测试**：
- 清空对话后，LLM 不应收到历史记录
- 正常对话时，历史应该正确传递
- 长对话时，历史应该被正确修剪
