# BaseSession.history 删除分析报告

## 一、当前架构分析

### 1.1 历史管理的双重机制

**发现**：系统中存在两套历史管理机制：

#### 机制 A：数据库历史（agent/session.py）
- **位置**：`MessageStore` 类
- **存储**：SQLite 数据库 `~/.niu/messages.db`
- **调用路径**：
  1. `niu_api/compat.py:134` → `store.get_messages(limit=None)`
  2. `niu_api/compat.py:139-143` → 转换为 `history_for_runner`
  3. `niu_api/compat.py:158` → 传递给 `runner.chat(..., history=history_for_runner)`
  4. `agent/runner.py:329` → 接收 `history` 参数
  5. `agent/runner.py:373` → 传递给 `agent_runner_loop(..., history=history)`
  6. `agent/generic/agent_loop.py:82-96` → 初始化 `messages`

#### 机制 B：内存历史（agent/generic/llmcore.py）
- **位置**：`BaseSession.history` 列表
- **存储**：内存列表
- **调用路径**：
  1. `BaseSession.ask(prompt)` (llmcore.py:651)
  2. `self.history.append({"role": "user", "content": ...})` (llmcore.py:655)
  3. `messages = self.make_messages(self.history)` (llmcore.py:657)
  4. `gen = self.raw_ask(messages, model)` (llmcore.py:664)

**核心冲突**：
- 数据库历史在 `agent_loop` 层管理
- 内存历史在 `BaseSession` 层管理
- 两者职责重叠，造成混乱

### 1.2 Line 178 覆盖 messages 的原因分析

**代码**：
```python
# agent/generic/agent_loop.py:178-180
messages = [
    {"role": "user", "content": next_prompt, "tool_results": tool_results}
]  # just new message, history is kept in *Session
```

**注释解读**：
- "just new message, history is kept in *Session"
- 说明设计意图是：agent_loop 只传递当前消息，历史由 BaseSession.history 管理

**证据**：
1. `client.chat(messages=messages)` (agent_loop.py:109)
   - 第一次调用时，messages 包含完整历史（从数据库加载）
   - 后续调用时，messages 被覆盖为只有当前消息
2. `ToolClient.chat(messages, tools)` (llmcore.py:963)
   - 接收 messages 参数
   - 调用 `self._build_protocol_prompt(messages, tools)` (llmcore.py:964)
   - 最终调用 `self.backend.ask(full_prompt, stream=True)` (llmcore.py:970)
   - `full_prompt` 是字符串，由 `_build_protocol_prompt` 构建
3. `BaseSession.ask(prompt)` (llmcore.py:651)
   - 接收字符串 prompt
   - 自己管理 history：`self.history.append(...)`
   - 构建 messages：`messages = self.make_messages(self.history)`

**结论**：Line 178 覆盖 messages 是**设计意图**，不是 Bug。

### 1.3 历史传递的完整流程

#### 工具循环（agent_loop）

```
第1轮：
  agent_loop: messages = [system, history..., user_input]  ← 数据库历史
  client.chat(messages)
  ↓
  ToolClient.chat(messages)
  ↓
  _build_protocol_prompt(messages) → full_prompt (字符串)
  ↓
  BaseSession.ask(full_prompt)  ← 接收字符串
  ↓
  BaseSession: self.history.append({"role": "user", "content": full_prompt})
  ↓
  BaseSession: messages = self.make_messages(self.history)  ← 内存历史
  ↓
  raw_ask(messages)

第2轮：
  agent_loop: messages = [{"role": "user", "content": next_prompt}]  ← 只有当前消息
  client.chat(messages)
  ↓
  ToolClient.chat(messages)
  ↓
  _build_protocol_prompt(messages) → full_prompt
  ↓
  BaseSession.ask(full_prompt)
  ↓
  BaseSession: self.history.append({"role": "user", "content": full_prompt})
  ↓
  BaseSession: messages = self.make_messages(self.history)  ← 完整历史（包括第1轮）
  ↓
  raw_ask(messages)
```

**关键发现**：
1. `agent_loop` 的 `messages` 在第1轮包含数据库历史
2. 第2轮被覆盖为只有当前消息
3. 但 `BaseSession.history` 会累积所有消息
4. `ToolClient._build_protocol_prompt` 只处理当前 messages，提取历史
5. 真正的历史管理在 `BaseSession.history`

**问题**：
- 数据库历史只在第1轮生效
- 后续轮次依赖 `BaseSession.history`
- 这导致了双重历史管理的混乱

### 1.4 Native 模式的历史传递

**NativeToolClient.chat(messages)** (llmcore.py:1380-1426)：
```python
def chat(self, messages, tools=None):
    combined_content = []
    tool_results = []
    for msg in messages:
        # 处理 messages，提取 tool_results
        ...
    merged = {"role": "user", "content": tool_result_blocks + combined_content}
    gen = self.backend.ask(merged, self.tools)  # ← 传递单个 dict
```

**NativeClaudeSession.ask(msg)** (llmcore.py:830-881)：
```python
def ask(self, msg, tools=None, model=None):
    assert type(msg) is dict
    with self.lock:
        self.history.append(msg)  # ← 追加到内存历史
        trim_messages_history(self.history, self.context_win)
        messages = list(self.history)  # ← 使用内存历史

    gen = self.raw_ask(messages, tools, self.system, model)
    ...
    self.history.append({"role": "assistant", "content": content_blocks})
```

**发现**：
- Native 模式也依赖 `BaseSession.history`（通过继承）
- 传递的是单个消息 dict
- `NativeClaudeSession.ask` 会将其追加到 `self.history`

### 1.5 自我进化如何使用历史

**handler.py:tool_after_callback**：
```python
def tool_after_callback(self, tool_name, args, response, ret):
    # ...
    summary = f"[{tool_name}] {summary}"
    self.history_info.append("[Agent] " + smart_format(summary, max_str_len=100))
```

**handler.py:_get_anchor_prompt**：
```python
def _get_anchor_prompt(self, turn):
    h_str = "\n".join(self.history_info[-20:])
    anchor_prompt = f"""
## 工作记忆（最近工具调用）

{h_str}
"""
    return anchor_prompt
```

**发现**：
- `self.history_info` 是工具调用摘要，不是对话历史
- 与 `BaseSession.history` 无关
- 与数据库历史也无关

## 二、删除 BaseSession.history 的必要性

### 2.1 为什么必须删除？

1. **重复管理**：
   - 数据库已经存储完整历史
   - `BaseSession.history` 是冗余的内存缓存
   - 两者不同步会导致混乱

2. **内存浪费**：
   - `BaseSession.history` 会无限增长
   - 虽然有 `trim_messages_history`，但仍然占用内存
   - 数据库已经是持久化存储，不需要内存缓存

3. **架构混乱**：
   - 数据库历史只在第1轮生效
   - 后续轮次依赖内存历史
   - 开发者难以理解历史来源

4. **难以维护**：
   - 两套历史管理逻辑
   - 修复问题时需要考虑两者同步
   - 容易引入 Bug

### 2.2 删除后应该由谁管理历史？

**答案**：`agent_loop` 管理历史，BaseSession 只负责 API 调用。

**理由**：
1. `agent_loop` 已经接收数据库历史（Line 82-96）
2. `agent_loop` 是工具循环的核心，应该管理消息流转
3. `BaseSession` 应该是纯粹的 API 调用层，不应该管理状态

## 三、完整改造方案

### 3.1 核心思路

**原则**：
1. `agent_loop` 维护完整的 messages 列表（包含历史）
2. 每轮调用 `client.chat(messages)` 时传递完整历史
3. `BaseSession` 不再管理历史，只接收 messages

**改造步骤**：

#### 步骤 1：修改 agent_loop.py 的 messages 管理

**当前代码（Line 178-180）**：
```python
messages = [
    {"role": "user", "content": next_prompt, "tool_results": tool_results}
]  # just new message, history is kept in *Session
```

**改为追加消息 + 裁剪**：
```python
# 追加 assistant 响应
messages.append({"role": "assistant", "content": response.content})

# 追加用户下一轮输入
messages.append({"role": "user", "content": next_prompt, "tool_results": tool_results})

# 裁剪历史（保留 system + 最近 N 轮对话）
MAX_HISTORY_TURNS = 20
if len(messages) > MAX_HISTORY_TURNS * 2 + 1:  # system + 历史对话
    # 保留 system 和最近的消息
    messages = messages[:1] + messages[-(MAX_HISTORY_TURNS * 2):]
```

**注意**：
- 需要追加 assistant 响应
- 需要追加用户下一轮输入
- 需要裁剪避免无限增长

#### 步骤 2：修改 BaseSession.ask 接收 messages

**当前代码（llmcore.py:651-683）**：
```python
def ask(self, prompt, model=None, stream=False):
    def _ask_gen():
        with self.lock:
            self.history.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
            trim_messages_history(self.history, self.context_win)
            messages = self.make_messages(self.history)
        # ...
```

**改为接收 messages**：
```python
def ask(self, messages, model=None, stream=False):
    def _ask_gen():
        # 不再管理 history，直接使用传入的 messages
        # messages 已经是完整的历史，包括当前用户输入
        gen = self.raw_ask(messages, model)
        # ...
        # 不再追加到 self.history
    # ...
```

**注意**：
- 删除 `self.history.append(...)`
- 删除 `trim_messages_history(self.history, ...)`
- 删除 `self.make_messages(self.history)`
- 直接使用传入的 messages

#### 步骤 3：修改 ToolClient.chat 传递 messages

**Non-Native 模式（llmcore.py:963-985）**：

**当前代码**：
```python
def chat(self, messages, tools=None):
    full_prompt = self._build_protocol_prompt(messages, tools)
    gen = self.backend.ask(full_prompt, stream=True)
```

**改为**：
```python
def chat(self, messages, tools=None):
    # messages 已经包含完整历史，直接传递
    gen = self.backend.ask(messages, stream=True)
```

**注意**：
- `_build_protocol_prompt` 可能还需要保留，用于转换格式
- 但不应该在 `BaseSession.ask` 中再次管理历史

#### 步骤 4：修改 NativeClaudeSession.ask 接收 messages

**当前代码（llmcore.py:830-881）**：
```python
def ask(self, msg, tools=None, model=None):
    assert type(msg) is dict
    with self.lock:
        self.history.append(msg)
        trim_messages_history(self.history, self.context_win)
        messages = list(self.history)
    # ...
```

**改为接收 messages**：
```python
def ask(self, messages, tools=None, model=None):
    # messages 已经是完整列表
    # 不再管理 history
    gen = self.raw_ask(messages, tools, self.system, model)
    # ...
    # 不再追加到 self.history
```

**注意**：
- Native 模式也需要接收 messages 列表
- `NativeToolClient.chat` 已经在处理 messages

#### 步骤 5：修改 NativeToolClient.chat

**当前代码（llmcore.py:1380-1426）**：
```python
def chat(self, messages, tools=None):
    # ...
    merged = {"role": "user", "content": tool_result_blocks + combined_content}
    gen = self.backend.ask(merged, self.tools)
```

**改为传递完整 messages**：
```python
def chat(self, messages, tools=None):
    # messages 已经是完整历史，包括 tool_results
    # 直接传递给 backend
    gen = self.backend.ask(messages, tools)
```

**注意**：
- Native 模式需要传递 messages 列表
- 不应该合并为单个 dict

#### 步骤 6：删除 BaseSession.history 相关代码

**需要删除的代码**：
1. `BaseSession.__init__` 中的 `self.history = []` (llmcore.py:632)
2. `BaseSession.ask` 中的历史管理代码 (llmcore.py:655-657, 679-681)
3. `NativeClaudeSession.ask` 中的历史管理代码 (llmcore.py:833-835, 849)
4. `clear_chat` 中清除 `runner.client.backend.history` 的代码 (niu_api/compat.py:274-276)

**需要保留的代码**：
- `MessageStore` 数据库管理
- `agent_loop` 的 messages 初始化和传递
- `runner.chat` 接收 history 参数并传递给 agent_loop

### 3.2 改造后的架构

```
用户输入
    ↓
niu_api/compat.py: 从数据库加载历史
    ↓
runner.chat(history=database_history)
    ↓
agent_runner_loop(history=database_history)
    ↓
messages = [system] + history + [current_user]
    ↓
while 循环：
    client.chat(messages)  ← 传递完整 messages
    ↓
    ToolClient.chat(messages)
    ↓
    BaseSession.ask(messages)  ← 接收 messages，不管理历史
    ↓
    raw_ask(messages)  ← 直接使用 messages
    ↓
    messages.append(assistant_response)  ← agent_loop 追加响应
    messages.append(next_user_input)  ← agent_loop 追加下一轮输入
    ↓
    裁剪 messages（保留最近 N 轮）
```

### 3.3 关键改动点

| 文件 | 行号 | 改动 | 原因 |
|------|------|------|------|
| agent_loop.py | 178-180 | 改为追加 + 裁剪 | 让 agent_loop 真正管理历史 |
| llmcore.py | 632 | 删除 `self.history = []` | 不再需要内存历史 |
| llmcore.py | 651-683 | 改为接收 messages | BaseSession 不管理历史 |
| llmcore.py | 830-881 | 改为接收 messages | Native 模式也不管理历史 |
| llmcore.py | 963-985 | 直接传递 messages | 不再转换格式 |
| llmcore.py | 1380-1426 | 传递完整 messages | Native 模式也使用完整历史 |
| niu_api/compat.py | 274-276 | 删除清除 history 代码 | 不再存在 history |

## 四、潜在问题和解决方案

### 4.1 问题：工具循环中 messages 会无限增长

**解决方案**：在 agent_loop 中裁剪

```python
# 每轮结束时裁剪
MAX_HISTORY_TURNS = 20
if len(messages) > MAX_HISTORY_TURNS * 2 + 1:
    messages = messages[:1] + messages[-(MAX_HISTORY_TURNS * 2):]
```

**注意**：
- 需要在追加消息后裁剪
- 需要保留 system 消息
- 需要保留最近 N 轮对话

### 4.2 问题：Non-Native 模式的 prompt 格式

**当前**：`_build_protocol_prompt` 将 messages 转换为字符串 prompt

**改造后**：
- 选项 A：保留 `_build_protocol_prompt`，但在 agent_loop 中调用，而不是在 BaseSession 中
- 选项 B：删除 `_build_protocol_prompt`，直接传递 messages 给 API

**建议**：选项 B，因为：
- 现代 LLM API 都支持 messages 格式
- 统一使用 messages 更清晰
- 避免 prompt 字符串拼接的复杂性

### 4.3 问题：Native 模式的 messages 格式

**当前**：`NativeToolClient.chat` 合并 messages 为单个 dict

**改造后**：直接传递 messages 列表

**注意**：
- `NativeClaudeSession.raw_ask` 需要支持 messages 列表
- 已经支持了（Line 835: `messages = list(self.history)`）

### 4.4 问题：自我进化依赖 history_info

**发现**：`handler.py` 的 `self.history_info` 是工具调用摘要，不是对话历史

**结论**：不受影响，不需要修改

### 4.5 问题：测试验证

**需要验证的功能**：
1. 工具循环是否正常？
   - 测试：多轮工具调用
   - 验证：messages 是否正确传递

2. 自我进化是否正常？
   - 测试：触发自我进化
   - 验证：history_info 是否正常

3. Native 和 Non-Native 模式是否都正常？
   - 测试：两种模式都运行一次
   - 验证：历史传递是否正确

4. 历史管理是否正确？
   - 测试：多轮对话
   - 验证：数据库历史是否正确，内存是否释放

## 五、改造步骤（按优先级）

### 5.1 Phase 1：修改 agent_loop（核心）

1. 修改 Line 178-180，改为追加 + 裁剪
2. 添加裁剪逻辑
3. 测试工具循环

### 5.2 Phase 2：修改 BaseSession（删除 history）

1. 删除 `self.history = []`
2. 修改 `ask` 方法接收 messages
3. 删除历史管理代码
4. 测试 Non-Native 模式

### 5.3 Phase 3：修改 Native 模式

1. 修改 `NativeClaudeSession.ask` 接收 messages
2. 修改 `NativeToolClient.chat` 传递 messages
3. 测试 Native 模式

### 5.4 Phase 4：清理和测试

1. 删除 `niu_api/compat.py` 中清除 history 的代码
2. 全面测试所有功能
3. 验证数据库历史正确性

## 六、风险评估

### 6.1 高风险点

1. **agent_loop 的 messages 管理**
   - 风险：追加和裁剪逻辑错误
   - 缓解：详细测试，打印 messages 长度

2. **Native 模式的 messages 格式**
   - 风险：Native API 不接受 messages 列表
   - 缓解：查阅 API 文档，测试验证

3. **Non-Native 模式的 prompt 格式**
   - 风险：某些 LLM 只接受字符串 prompt
   - 缓解：保留 `_build_protocol_prompt` 作为选项

### 6.2 中风险点

1. **工具循环的历史传递**
   - 风险：历史丢失或重复
   - 缓解：单元测试，打印 messages 内容

2. **内存管理**
   - 风险：裁剪不当导致内存泄漏
   - 缓解：监控内存使用，调整 MAX_HISTORY_TURNS

### 6.3 低风险点

1. **自我进化功能**
   - 风险：不影响
   - 缓解：测试验证

2. **数据库历史**
   - 风险：不变
   - 缓解：无需修改

## 七、回答用户的核心问题

### Q1: Line 178 覆盖 messages 是否是 Bug？

**答案**：不是 Bug，是设计意图。

**证据**：
- 注释明确说明："just new message, history is kept in *Session"
- 当前设计是 `agent_loop` 只传递当前消息，历史由 `BaseSession.history` 管理

**但是**：这个设计导致了双重历史管理，应该改造。

### Q2: 如果改为追加，会不会有其他问题？

**答案**：会有问题，但可以解决。

**问题**：
1. messages 会无限增长 → 需要裁剪
2. 需要追加 assistant 响应 → 当前只追加 user 输入
3. 需要处理 tool_results → 已经在 messages 中

**解决方案**：
```python
# 追加 assistant 响应
messages.append({"role": "assistant", "content": response.content})

# 追加用户下一轮输入
messages.append({"role": "user", "content": next_prompt, "tool_results": tool_results})

# 裁剪历史
MAX_HISTORY_TURNS = 20
if len(messages) > MAX_HISTORY_TURNS * 2 + 1:
    messages = messages[:1] + messages[-(MAX_HISTORY_TURNS * 2):]
```

### Q3: BaseSession 是否应该接收 messages 而不是 prompt？

**答案**：是的，BaseSession 应该接收 messages。

**理由**：
1. `agent_loop` 已经管理了完整的历史 messages
2. `BaseSession` 不应该再管理历史
3. `BaseSession` 应该是纯粹的 API 调用层

**改造**：
```python
# 当前
def ask(self, prompt, model=None, stream=False):
    # prompt 是字符串

# 改为
def ask(self, messages, model=None, stream=False):
    # messages 是列表
```

### Q4: 完整的改造步骤是什么？

**答案**：见"三、完整改造方案"章节。

**核心步骤**：
1. 修改 `agent_loop.py` 的 messages 管理（追加 + 裁剪）
2. 修改 `BaseSession.ask` 接收 messages
3. 修改 `ToolClient.chat` 传递 messages
4. 修改 `NativeClaudeSession.ask` 接收 messages
5. 修改 `NativeToolClient.chat` 传递 messages
6. 删除 `BaseSession.history` 相关代码

### Q5: 改造后如何验证？

**答案**：需要验证以下功能：

**功能验证**：
1. **工具循环**：
   - 测试：执行需要多轮工具调用的任务
   - 验证：工具调用是否正常，历史是否正确传递
   - 方法：打印 messages 长度和内容

2. **自我进化**：
   - 测试：触发自我进化（35轮或7轮）
   - 验证：是否正常询问用户或警告
   - 方法：检查 handler.history_info

3. **Native 模式**：
   - 测试：使用 Native Claude 或 Native OpenAI
   - 验证：历史传递是否正确
   - 方法：检查 API 请求日志

4. **Non-Native 模式**：
   - 测试：使用 OpenAI 或其他 LLM
   - 验证：历史传递是否正确
   - 方法：检查 prompt 长度

5. **数据库历史**：
   - 测试：多轮对话后重启
   - 验证：历史是否正确加载
   - 方法：检查数据库内容

**验证方法**：
1. 单元测试：编写测试用例
2. 日志验证：添加 debug 日志
3. 手动测试：实际对话测试
4. 性能测试：监控内存使用

## 八、总结

**核心结论**：
1. Line 178 覆盖 messages 不是 Bug，但设计不合理
2. 应该让 `agent_loop` 真正管理历史
3. `BaseSession.history` 是冗余的，应该删除
4. 改造需要分步骤，逐步验证

**关键收益**：
1. 统一历史管理，避免混乱
2. 减少内存占用
3. 架构更清晰
4. 易于维护

**风险**：
1. 需要仔细处理 messages 裁剪
2. 需要验证所有功能正常
3. 需要测试 Native 和 Non-Native 模式

**建议**：
1. 先在开发环境测试
2. 详细测试所有功能
3. 监控内存使用
4. 保留回滚方案
