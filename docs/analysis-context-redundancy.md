# 上下文管理冗余层分析

## 当前架构：三层存储

```
┌─────────────────────────────────────────────────────────────┐
│                         用户输入                              │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  【第 1 层】数据库持久化 (agent/session.py)                    │
│  - MessageStore (SQLite)                                     │
│  - store.get_messages(limit=100)                             │
│  - 唯一的持久化层                                             │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  【第 2 层】compat.py 临时变量                                 │
│  - history = await store.get_messages(limit=100)             │
│  - history_for_runner = [...]                                │
│  - 传递给 runner.chat(..., history=history_for_runner)        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  【第 3 层】BaseSession.history (agent/generic/llmcore.py)    │
│  - self.history = []  (内存缓存)                              │
│  - 在 BaseSession.ask() 中累积                                │
│  - ❌ 这是冗余层！                                            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                     发送给 LLM                                │
└─────────────────────────────────────────────────────────────┘
```

## 详细消息流动路径

### 正常对话流程

```python
# 1. 用户输入消息
用户: "你好"

# 2. niu_api/compat.py: chat_session()
store = await get_message_store()
await store.add_message(role="user", content="你好")

# 3. 从数据库加载历史（包含之前的所有对话）
history = await store.get_messages(limit=100)
# history = [msg1, msg2, ..., msgN, 当前消息]

history_for_runner = [
    {"role": msg.role, "content": msg.content}
    for msg in history[:-1]  # 排除当前消息
]

# 4. 传递给 runner
runner.chat(session_id, request.message, history=history_for_runner)

# 5. agent/runner.py: NiuRunner.chat()
gen = agent_runner_loop(
    client=self.client,
    system_prompt=system_prompt,
    user_input=user_input,
    handler=self.handler,
    tools_schema=tools_schema,
    history=history,  # ← 数据库历史传入
)

# 6. agent/generic/agent_loop.py: agent_runner_loop()
messages = [{"role": "system", "content": system_prompt}]

# 添加数据库历史
if history:
    for msg in history:
        messages.append({"role": msg["role"], "content": msg["content"]})

# 添加当前用户消息
messages.append({"role": "user", "content": user_input})

# 此时 messages = [system, history1, history2, ..., 当前消息]

# 7. 调用 client.chat()
response_gen = client.chat(messages=messages, tools=tools_schema)

# 8. agent/generic/llmcore.py: ToolClient.chat()
full_prompt = self._build_protocol_prompt(messages, tools)

# _build_protocol_prompt() 的实现：
def _build_protocol_prompt(self, messages, tools):
    # 提取 system prompt
    system_from_messages = next(
        (m["content"] for m in messages if m["role"].lower() == "system"),
        ""
    )
    if system_from_messages:
        self._system_prompt = system_from_messages
    system_content = self._system_prompt

    # 提取历史消息（非 system）
    history_msgs = [m for m in messages if m["role"].lower() != "system"]

    # 构建 prompt
    tool_instruction = self._prepare_tool_instruction(tools)
    system = f"{system_content}\n{tool_instruction}"
    user = ""

    # 遍历历史消息
    for m in history_msgs:
        role = "USER" if m["role"] == "user" else "ASSISTANT"
        user += f"=== {role} ===\n"
        user += str(m["content"]) + "\n"

    user += "=== ASSISTANT ===\n"
    return system + user

# 此时 full_prompt = system + 历史对话 + "=== ASSISTANT ==="

# 9. 调用 backend.ask()
gen = self.backend.ask(full_prompt, stream=True)

# 10. agent/generic/llmcore.py: BaseSession.ask()
def ask(self, prompt, model=None, stream=False):
    def _ask_gen():
        content = ""
        with self.lock:
            # ❌ 问题：这里又把 prompt 追加到 self.history
            self.history.append({
                "role": "user",
                "content": [{"type": "text", "text": prompt}]
            })
            trim_messages_history(self.history, self.context_win)
            messages = self.make_messages(self.history)

        # 发送给 LLM
        content_blocks = None
        gen = self.raw_ask(messages, model)
        # ...

        # ❌ 问题：这里又把响应追加到 self.history
        if not content.startswith("Error:"):
            self.history.append({
                "role": "assistant",
                "content": [{"type": "text", "text": content}]
            })

    return _ask_gen() if stream else "".join(list(_ask_gen()))
```

### `/new` 清空流程

```python
# niu_api/compat.py: clear_chat()
@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    # 1. 清空数据库 ✅
    store = await get_message_store()
    count = await store.clear_messages()

    # 2. 重置 runner 状态
    runner = get_or_create_runner()
    if runner:
        # 重置 handler 的工作记忆 ✅
        if runner.handler:
            runner.handler.reset_working_memory()

        # 重置 LLM session 的 history（内存缓存）❓
        if runner.client and hasattr(runner.client, 'backend'):
            if hasattr(runner.client.backend, 'history'):
                runner.client.backend.history = []
                logger.info("Cleared LLM session history")

    return {"success": True, "deleted_count": count}
```

## 问题分析

### 问题 1：`BaseSession.history` 是冗余层

**现状**：
- 数据库已经存储了完整的历史对话
- `compat.py` 每次从数据库加载历史并传递给 `agent_loop`
- `agent_loop` 将历史构建成 `messages` 并传递给 `ToolClient.chat()`
- `ToolClient._build_protocol_prompt()` 将 `messages` 转换成 prompt 字符串
- **但是**：`BaseSession.ask()` 又把整个 prompt 字符串追加到 `self.history`

**冗余表现**：
```python
# 第一次对话
用户: "你好"
→ 数据库: [user: "你好"]
→ BaseSession.history: [user: "system + 你好"]
→ 发送给 LLM: "system + 你好"

# 第二次对话
用户: "今天天气怎么样？"
→ 数据库: [user: "你好", assistant: "你好！", user: "今天天气怎么样？"]
→ compat.py 加载历史: ["你好", "你好！", "今天天气怎么样？"]
→ agent_loop 构建 messages: [system, "你好", "你好！", "今天天气怎么样？"]
→ ToolClient 构建 prompt: "system + 你好 + 你好！ + 今天天气怎么样？"
→ BaseSession.ask():
   self.history.append({user: "system + 你好 + 你好！ + 今天天气怎么样？"})
   # ❌ 这里重复了！prompt 已经包含了历史，但又追加到 history

# 结果：BaseSession.history 和数据库历史重复存储！
```

### 问题 2：`/new` 清空不彻底

**清空逻辑**：
```python
# 清空数据库 ✅
await store.clear_messages()

# 尝试清空 BaseSession.history ❓
if runner.client and hasattr(runner.client, 'backend'):
    if hasattr(runner.client.backend, 'history'):
        runner.client.backend.history = []
```

**潜在问题**：
1. 属性访问路径可能不正确
   - `runner.client` 是 `ToolClient` 或 `NativeToolClient`
   - `runner.client.backend` 是 `BaseSession` 子类
   - 需要确认 `hasattr(runner.client, 'backend')` 是否为 `True`

2. 即使清空了，下次对话时：
   ```python
   # compat.py: chat_session()
   history = await store.get_messages(limit=100)  # 从数据库加载历史

   # 如果数据库被清空，history = []
   # 传递给 runner.chat(..., history=[])
   # agent_loop 构建 messages = [system, 当前消息]
   # ToolClient 构建 prompt = "system + 当前消息"
   # BaseSession.ask():
   #   self.history.append({user: "system + 当前消息"})
   #   # ✅ 这次是对的
   ```

   **但是**，如果数据库清空了，但 `BaseSession.history` 没清空：
   ```python
   # BaseSession.history = [之前的对话...]  # ❌ 没清空

   # agent_loop 传入 history=[] (从数据库加载)
   # agent_loop 构建 messages = [system, 当前消息]
   # ToolClient 构建 prompt = "system + 当前消息"

   # BaseSession.ask():
   #   self.history.append({user: "system + 当前消息"})
   #   # ❌ self.history 还包含之前的对话！
   #   messages = self.make_messages(self.history)  # 包含历史对话
   #   raw_ask(messages, model)  # 发送给 LLM 的消息包含历史！
   ```

### 问题 3：`BaseSession.history` 和数据库历史的语义冲突

**BaseSession.history 的原始设计意图**：
- 在 `BaseSession.ask()` 中累积对话历史
- 用于上下文窗口管理 (`trim_messages_history`)
- 每次 `ask()` 时，`self.history` 包含完整的对话历史

**当前架构的实际行为**：
- 数据库存储原始消息：`[user: "你好", assistant: "你好！", user: "今天天气？"]`
- `compat.py` 从数据库加载并传递给 `agent_loop`
- `agent_loop` 构建包含历史的 `messages`
- `ToolClient._build_protocol_prompt()` 将 `messages` 转换成 prompt 字符串
- **关键问题**：`BaseSession.ask()` 接收的是 **整个 prompt 字符串**，而不是单条消息

**语义混乱**：
```python
# BaseSession.ask() 的参数 prompt 是什么？
prompt = "system + 历史对话 + 当前用户输入"

# BaseSession.history.append({user: prompt})
# 这里把整个 prompt 当作一条 user 消息追加！
# ❌ 这是错误的语义！

# BaseSession.history 应该存储的是：
# [user: "你好", assistant: "你好！", user: "今天天气？"]
# 而不是：
# [user: "system + 你好", user: "system + 你好 + 你好！ + 今天天气？"]
```

## 根本原因

**ToolClient 和 BaseSession 的设计不匹配**：

1. **ToolClient 的设计**：
   - 每次调用 `chat(messages, tools)` 时，`messages` 包含完整的对话历史
   - `_build_protocol_prompt()` 将 `messages` 转换成 prompt 字符串
   - **不需要** `BaseSession.history` 来存储历史

2. **BaseSession 的设计**：
   - 原本设计为：每次 `ask(prompt)` 时，`prompt` 是单条用户消息
   - `self.history` 累积对话历史
   - **不适合** 接收已经包含历史的 prompt 字符串

3. **实际使用**：
   - `ToolClient.chat()` 传入包含历史的 `messages`
   - 构建成 prompt 字符串后传给 `BaseSession.ask()`
   - `BaseSession.ask()` 把整个 prompt 当作一条消息追加到 `self.history`
   - **导致**：`BaseSession.history` 累积的是 prompt 字符串，而不是原始消息

## 解决方案

### 方案 1：去掉 `BaseSession.history`（推荐）

**原则**：数据库是唯一的真实来源

**修改**：
1. `BaseSession.ask()` 不再维护 `self.history`
2. `ToolClient._build_protocol_prompt()` 负责构建包含历史的 prompt
3. 数据库历史通过 `compat.py` → `agent_loop` → `ToolClient` 传递

**优点**：
- 单一真实来源（数据库）
- 无冗余存储
- `/new` 清空数据库即可，无需清理内存缓存

**缺点**：
- 需要修改 `BaseSession` 和 `ToolClient`
- 可能影响其他使用 `BaseSession` 的代码

### 方案 2：保留 `BaseSession.history` 但正确同步

**原则**：`BaseSession.history` 是数据库的内存缓存

**修改**：
1. `BaseSession.history` 存储原始消息（不是 prompt 字符串）
2. `compat.py` 在加载历史后，同步到 `BaseSession.history`
3. `/new` 清空时，同时清空数据库和 `BaseSession.history`

**优点**：
- 兼容现有代码
- `BaseSession` 可以独立使用（不依赖数据库）

**缺点**：
- 仍然有冗余层
- 需要维护同步逻辑

### 方案 3：分离 `BaseSession` 和 `ToolClient` 的职责

**原则**：各司其职，不混淆

**修改**：
1. `BaseSession` 只负责发送请求给 LLM，不维护历史
2. `ToolClient` 负责构建 prompt（包含历史）
3. 数据库负责持久化历史

**优点**：
- 职责清晰
- 易于维护

**缺点**：
- 需要重构 `BaseSession`

## 推荐方案：方案 1（去掉 `BaseSession.history`）

### 实施步骤

#### 1. 修改 `BaseSession.ask()` - 不再维护 `self.history`

```python
# agent/generic/llmcore.py

class BaseSession:
    def __init__(self, cfg):
        # ... 其他初始化
        # ❌ 删除：self.history = []
        self.lock = threading.Lock()
        # ...

    def ask(self, prompt, model=None, stream=False):
        def _ask_gen():
            content = ""
            with self.lock:
                # ❌ 删除：self.history.append(...)
                # ❌ 删除：trim_messages_history(self.history, self.context_win)

                # 直接使用 prompt 构建消息
                # 注意：prompt 可能是字符串，也可能是包含历史的 messages
                # 这里需要根据实际情况调整

            # ... 其他逻辑
            # ❌ 删除：self.history.append(...)

        return _ask_gen() if stream else "".join(list(_ask_gen()))
```

#### 2. 修改 `ToolClient.chat()` - 直接构建 prompt

```python
# agent/generic/llmcore.py

class ToolClient:
    def chat(self, messages, tools=None):
        full_prompt = self._build_protocol_prompt(messages, tools)

        # ❌ 不再调用 backend.ask(prompt)
        # ✅ 改为：直接发送 prompt 给 backend
        gen = self.backend.ask(full_prompt, stream=True)

        # ... 其他逻辑
```

#### 3. 修改 `NativeToolClient.chat()` - 适配新架构

```python
# agent/generic/llmcore.py

class NativeToolClient:
    def chat(self, messages, tools=None):
        # ✅ messages 包含完整历史，直接使用
        # ... 构建 combined_content

        merged = {"role": "user", "content": combined_content}
        gen = self.backend.ask(merged, self.tools)

        # ... 其他逻辑
```

#### 4. 修改 `/new` 清空逻辑 - 只清空数据库

```python
# niu_api/compat.py

@router.post("/api/chat/clear")
async def clear_chat() -> dict:
    # ✅ 只清空数据库
    store = await get_message_store()
    count = await store.clear_messages()

    # ❌ 删除：重置 BaseSession.history 的逻辑

    # ✅ 保留：重置 handler 的工作记忆
    runner = get_or_create_runner()
    if runner and runner.handler:
        runner.handler.reset_working_memory()

    return {"success": True, "deleted_count": count}
```

### 验证步骤

1. **测试正常对话**：
   - 发送多条消息
   - 检查 LLM prompt 是否包含历史（应该包含）
   - 检查数据库是否存储消息

2. **测试 `/new` 清空**：
   - 执行 `/new`
   - 发送新消息
   - 检查 LLM prompt 是否包含之前的历史（不应该包含）

3. **检查 `BaseSession.history` 是否被使用**：
   - 搜索代码中是否有其他地方访问 `session.history`
   - 如果有，需要调整

## 总结

**当前问题**：
- 三层存储导致冗余和混乱
- `/new` 清空不彻底
- `BaseSession.history` 和数据库历史语义冲突

**根本原因**：
- `ToolClient` 和 `BaseSession` 设计不匹配
- `BaseSession.ask()` 接收的是包含历史的 prompt，而不是单条消息

**推荐方案**：
- 去掉 `BaseSession.history`
- 数据库作为唯一真实来源
- 每次对话从数据库加载历史并构建 prompt

**核心原则**：
- 数据库 = 持久化层
- 内存 = 运行时缓存（可选，通过 `compat.py` 的临时变量实现）
- 不需要第三层存储
