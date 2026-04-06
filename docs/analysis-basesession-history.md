# BaseSession.history 深度分析报告

## 执行摘要

**结论**：`BaseSession.history` **不是冗余层**，它是 GenericAgent 的**核心内存管理组件**，负责多轮工具调用的上下文传递和自动上下文窗口管理。

**当前问题的根源**：不是"有三层 history"，而是"三层没有正确协作"。NiuRunner 在新会话开始时没有正确初始化 BaseSession.history。

---

## 1. 工具调用循环中的 history

### agent_runner_loop 的设计

从 `agent/generic/agent_loop.py` 第 70-182 行：

```python
def agent_runner_loop(
    client,
    system_prompt,
    user_input,
    handler,
    tools_schema,
    max_turns=40,
    verbose=True,
    initial_user_content=None,
    history=None,  # Optional: 初始历史
):
    # 第一轮：构建完整的 messages
    messages = [{"role": "system", "content": system_prompt}]
    
    if history:
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
    
    messages.append({
        "role": "user",
        "content": initial_user_content if initial_user_content is not None else user_input,
    })
    
    # 多轮循环
    while turn < handler.max_turns:
        # ...
        
        # 关键：后续轮次只传递新消息！
        messages = [
            {"role": "user", "content": next_prompt, "tool_results": tool_results}
        ]  # just new message, history is kept in *Session
```

**核心发现**：

1. **第一轮**：`agent_runner_loop` 构建完整的 messages（system + history + user）
2. **后续轮次**：只构建新的 user 消息（next_prompt + tool_results）
3. **注释明确说明**："just new message, history is kept in *Session"

这意味着：
- `agent_runner_loop` **不维护历史**，它在多轮之间只传递最新的消息
- **历史由 `*Session`（BaseSession）维护**

### BaseSession 如何维护历史

从 `agent/generic/llmcore.py` 第 658-690 行：

```python
class BaseSession:
    def ask(self, prompt, model=None, stream=False):
        def _ask_gen():
            content = ""
            with self.lock:
                # 1. 追加用户消息
                self.history.append({"role": "user", "content": [{"type": "text", "text": prompt}]})
                
                # 2. 自动压缩（上下文窗口管理）
                trim_messages_history(self.history, self.context_win)
                
                # 3. 用完整历史构建 messages
                messages = self.make_messages(self.history)
            
            # 4. 调用 LLM
            gen = self.raw_ask(messages, model)
            # ...
            
            # 5. 追加助手回复
            if not content.startswith("Error:"):
                self.history.append(
                    {"role": "assistant", "content": [{"type": "text", "text": content}]}
                )
        
        return _ask_gen() if stream else "".join(list(_ask_gen()))
```

**核心机制**：

1. 每次调用 `ask()`，都会：
   - 追加用户消息到 `self.history`
   - 调用 `trim_messages_history` 进行压缩（如果超出上下文窗口）
   - 用 `self.history` 构建完整的 messages
   - 追加助手回复到 `self.history`

2. 多轮工具调用流程：
   ```
   第1轮：
   - agent_runner_loop 构建完整 messages (system + history + user)
   - 调用 client.chat(messages)
   - ToolClient 传递给 backend.ask()
   - BaseSession.ask() 追加消息到 self.history
   
   第2轮：
   - agent_runner_loop 只构建新消息 (next_prompt + tool_results)
   - 调用 client.chat(messages)
   - ToolClient._build_protocol_prompt() 从 messages 中提取
   - BaseSession.ask() 将新消息追加到 self.history（历史在累积！）
   
   第3轮：
   - agent_runner_loop 继续构建新消息
   - BaseSession.history 包含第1、2、3轮的所有消息
   ```

**结论**：`BaseSession.history` 是多轮工具调用的**状态存储**，没有它，多轮工具调用无法工作。

---

## 2. 自动上下文窗口管理

从 `agent/generic/llmcore.py` 第 98-112 行：

```python
def trim_messages_history(history, context_win):
    compress_history_tags(history)
    cost = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
    print(f"[Debug] Current context: {cost} chars, {len(history)} messages.")
    
    # 如果超出限制，从最早的消息开始删除
    if cost > context_win * 3:
        target = context_win * 3 * 0.6
        while len(history) > 5 and cost > target:
            history.pop(0)  # 删除最早的消息
            
            # 确保每轮以 user 消息开头
            while history and history[0].get("role") != "user":
                history.pop(0)
            
            if history and history[0].get("role") == "user":
                history[0] = _sanitize_leading_user_msg(history[0])
            
            cost = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
        
        print(f"[Debug] Trimmed context, current: {cost} chars, {len(history)} messages.")
```

**核心功能**：

1. **自动压缩**：当 history 超出上下文窗口限制时，自动删除最早的消息
2. **保证结构**：确保每轮对话以 user 消息开头，清理孤立的 tool_result
3. **节省 tokens**：压缩 <thinking>、<tool_use>、<tool_result> 标签

**这是 GenericAgent 的关键能力**：
- 不需要外部干预，自动管理上下文窗口
- 保证多轮工具调用不会超出模型限制

**没有 BaseSession.history，这个机制无法工作！**

---

## 3. 自我进化机制的依赖

从 `agent/handler.py` 第 229-363 行：

### 3.1 工作记忆记录

```python
def tool_after_callback(self, tool_name, args, response, ret):
    """工具调用后记录摘要到 history_info"""
    
    # 提取 <summary> 标签或自动生成摘要
    content = getattr(response, "content", "") if response else ""
    rsumm = re.search(r"<summary>(.*?)</summary>", content, re.DOTALL)
    if rsumm:
        summary = rsumm.group(1).strip()[:200]
    else:
        clean_args = {k: v for k, v in args.items() if not k.startswith("_")}
        summary = f"调用工具{tool_name}, args: {clean_args}"
        if tool_name == "no_tool":
            summary = "直接回答了用户问题"
    
    # 记录到 history_info
    self.history_info.append("[Agent] " + summary[:100])
```

**作用**：生成工作记忆摘要，但这些摘要是**嵌入到 next_prompt** 中的，最终会追加到 `BaseSession.history`。

### 3.2 长期记忆提取

```python
def next_prompt_patcher(self, next_prompt, outcome, turn):
    """周期性警告和全局记忆注入"""
    
    # 每 5 轮注入相关长期记忆
    if turn % 5 == 0 and turn > 0:
        memories = self._recall_relevant_memories(next_prompt)
        if memories:
            next_prompt += f"\n\n### [相关长期记忆]\n{memories}"
    
    # 每 10 轮注入全局记忆
    if turn % 10 == 0 and turn > 0:
        global_mem = get_global_memory()
        if global_mem:
            next_prompt += f"\n\n### [GLOBAL MEMORY]\n{global_mem}"
```

**作用**：从向量库检索长期记忆，注入到 `next_prompt`，这些内容最终也会进入 `BaseSession.history`。

**结论**：自我进化机制**依赖** `BaseSession.history`：
- 工作记忆摘要被记录到 history
- 长期记忆被注入到 history
- 记忆压缩（start_long_term_update）从 history_info 提取内容

---

## 4. 两套 history 的真实关系

### 4.1 数据库 history（MessageStore）

**位置**：`niu_api/session.py`

**职责**：
- **持久化**：保存完整的对话历史到数据库
- **前端展示**：提供给前端渲染历史消息
- **会话恢复**：新会话开始时加载历史

**数据结构**：
```python
{
    "id": "msg_123",
    "role": "user/assistant",
    "content": "消息内容",
    "timestamp": "2024-01-01 12:00:00",
    "session_id": "session_456"
}
```

### 4.2 内存 history（BaseSession.history）

**位置**：`agent/generic/llmcore.py`

**职责**：
- **LLM 调用**：构建发送给 LLM 的完整 messages
- **上下文管理**：自动压缩、trim_messages_history
- **多轮工具调用**：记录 tool_result、维护对话状态

**数据结构**：
```python
[
    {"role": "system", "content": "系统提示词"},
    {"role": "user", "content": [{"type": "text", "text": "用户输入"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "助手回复"}]},
    {"role": "user", "content": "next_prompt", "tool_results": [...]},
    {"role": "assistant", "content": [{"type": "tool_use", ...}]},
]
```

### 4.3 关键区别

| 维度 | 数据库 history | 内存 history |
|------|----------------|--------------|
| **存储位置** | SQLite 数据库 | Python 内存 |
| **生命周期** | 持久化 | Runner 实例生命周期 |
| **内容格式** | 扁平的消息列表 | Claude/OpenAI content blocks |
| **主要用途** | 持久化、前端展示 | LLM API 调用、上下文管理 |
| **压缩策略** | 无（保存完整历史） | 自动压缩（trim_messages_history） |
| **包含工具调用细节** | 是（tool_results） | 是（完整的 tool_use/tool_result blocks） |

---

## 5. 当前架构的设计意图

### GenericAgent 为什么需要自己的 history？

**答案**：GenericAgent 是一个**独立的、可复用的 Agent 框架**，它不应该依赖外部持久化层。

**设计意图**：

1. **解耦**：GenericAgent 不关心如何持久化历史，只关心如何管理对话状态
2. **自动管理**：内置上下文窗口管理（trim_messages_history），无需外部干预
3. **可组合**：可以嵌入到任何系统中（CLI、Web、API），外部系统负责持久化

**正确的协作方式**：

```
外部系统（NiuRunner）                GenericAgent
┌─────────────────────┐            ┌──────────────────┐
│ 数据库 history      │            │ BaseSession.history │
│ (MessageStore)      │            │ (内存)            │
│                     │            │                  │
│ 会话开始时加载 ─────┼──────────> │ 初始化 history   │
│                     │            │                  │
│                     │            │ 多轮工具调用     │
│                     │            │ 自动压缩         │
│                     │            │                  │
│ 对话结束后保存 <────┼────────── │ 从 history 提取  │
└─────────────────────┘            └──────────────────┘
```

---

## 6. 当前架构的真正问题

### 问题 1：NiuRunner 没有正确初始化 BaseSession.history

**现状**：

```python
# agent/runner.py 第 328-374 行
def chat(self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None):
    # ...
    gen = agent_runner_loop(
        client=self.client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=self.handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        verbose=False,
        initial_user_content=user_input,
        history=history,  # Pass history to agent_loop
    )
```

**问题**：
- `history` 参数只在 `agent_runner_loop` 的第一轮使用
- 后续轮次依赖 `BaseSession.history`，但 `BaseSession.history` 在新会话开始时是空的
- 导致多轮工具调用的上下文丢失

**根本原因**：`agent_runner_loop` 的设计是**假设 BaseSession.history 已经初始化**，但 NiuRunner 没有正确初始化。

### 问题 2：ToolClient 的 history 管理

**现状**：

```python
# agent/generic/llmcore.py 第 1038-1062 行
def _build_protocol_prompt(self, messages, tools):
    # 提取 system prompt（第一次保存，之后复用）
    system_from_messages = next((m["content"] for m in messages if m["role"].lower() == "system"), "")
    if system_from_messages:
        self._system_prompt = system_from_messages
    system_content = self._system_prompt
    
    history_msgs = [m for m in messages if m["role"].lower() != "system"]
    # ...
    
    for m in history_msgs:
        role = "USER" if m["role"] == "user" else "ASSISTANT"
        user += f"=== {role} ===\n"
        # ...
    
    user += "=== ASSISTANT ===\n"
    return system + user
```

**问题**：
- `ToolClient._build_protocol_prompt()` 只处理传入的 `messages`，不维护历史
- 它依赖 `BaseSession.history` 来维护完整历史
- 但在新会话开始时，`BaseSession.history` 是空的

### 问题 3：会话恢复时的 history 同步

**现状**：
- 用户刷新页面后，前端从数据库加载 history
- 但 `BaseSession.history` 是空的，导致多轮工具调用的上下文丢失
- 需要手动从数据库加载 history 到 `BaseSession.history`

---

## 7. 正确的修复方案

### 方案 A：在 NiuRunner 中正确初始化 BaseSession.history

**实现步骤**：

1. **在 NiuRunner.chat() 中**：
   ```python
   def chat(self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None):
       # 如果有历史，初始化 BaseSession.history
       if history and len(self.client.backend.history) == 0:
           # 转换为 BaseSession 格式
           for msg in history:
               self.client.backend.history.append({
                   "role": msg["role"],
                   "content": [{"type": "text", "text": msg["content"]}]
               })
       
       # 调用 agent_runner_loop
       gen = agent_runner_loop(...)
   ```

2. **在 agent_runner_loop 中**：
   - 移除 `history` 参数（或保留用于调试）
   - 完全依赖 `BaseSession.history` 维护历史

**优点**：
- 保持 GenericAgent 的独立性
- 正确初始化 BaseSession.history
- 多轮工具调用正常工作

**缺点**：
- 需要在 NiuRunner 中处理 history 格式转换

### 方案 B：为 BaseSession 添加 `load_history()` 方法

**实现步骤**：

1. **在 BaseSession 中**：
   ```python
   class BaseSession:
       def load_history(self, history: list):
           """从外部加载历史（会话恢复时使用）"""
           with self.lock:
               self.history = []
               for msg in history:
                   self.history.append({
                       "role": msg["role"],
                       "content": [{"type": "text", "text": msg["content"]}]
                   })
   ```

2. **在 NiuRunner 中**：
   ```python
   def chat(self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None):
       # 加载历史到 BaseSession
       if history and len(self.client.backend.history) == 0:
           self.client.backend.load_history(history)
       
       # 调用 agent_runner_loop
       gen = agent_runner_loop(...)
   ```

**优点**：
- 清晰的职责划分
- 支持会话恢复
- 易于测试

**缺点**：
- 需要修改 BaseSession 接口

### 方案 C：调整 agent_runner_loop 的 history 处理

**实现步骤**：

1. **在 agent_runner_loop 中**：
   ```python
   def agent_runner_loop(...):
       # 只在第一轮处理 history（传递给 BaseSession）
       if history and turn == 1:
           # 初始化 BaseSession.history
           client.backend.history = []
           for msg in history:
               client.backend.history.append({
                   "role": msg["role"],
                   "content": [{"type": "text", "text": msg["content"]}]
               })
       
       # 后续完全依赖 BaseSession.history
       # ...
   ```

**优点**：
- 集中处理 history 初始化
- 不需要修改 NiuRunner

**缺点**：
- agent_runner_loop 需要访问 client.backend，增加耦合

---

## 8. 推荐方案

**推荐：方案 B（为 BaseSession 添加 `load_history()` 方法）**

**理由**：

1. **清晰的职责**：
   - BaseSession：管理对话历史（加载、追加、压缩）
   - NiuRunner：协调数据库和 BaseSession
   - agent_runner_loop：执行工具调用循环

2. **易于实现**：
   - 只需添加一个方法
   - 不破坏现有架构

3. **易于测试**：
   - 可以单独测试 load_history()
   - 可以模拟会话恢复场景

**实现细节**：

```python
# agent/generic/llmcore.py
class BaseSession:
    def load_history(self, history: list):
        """
        从外部加载历史（会话恢复时使用）
        
        Args:
            history: [{"role": "user/assistant", "content": "消息内容"}, ...]
        """
        with self.lock:
            self.history = []
            for msg in history:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                
                # 转换为 BaseSession 格式
                if isinstance(content, str):
                    content = [{"type": "text", "text": content}]
                
                self.history.append({
                    "role": role,
                    "content": content
                })
            
            print(f"[BaseSession] Loaded {len(self.history)} messages from external history")

# agent/runner.py
class NiuRunner:
    def chat(self, session_id: str, user_input: str, stream: bool = True, max_turns: int = 40, history: list = None):
        # 如果有历史且 BaseSession.history 为空，加载历史
        if history and len(self.client.backend.history) == 0:
            self.client.backend.load_history(history)
        
        # 调用 agent_runner_loop（不再需要 history 参数）
        gen = agent_runner_loop(
            client=self.client,
            system_prompt=system_prompt,
            user_input=user_input,
            handler=self.handler,
            tools_schema=tools_schema,
            max_turns=max_turns,
            verbose=False,
            initial_user_content=user_input,
            # history=history,  # 不再需要
        )
```

---

## 9. 总结

### BaseSession.history 的核心作用

1. **多轮工具调用的状态存储**：维护完整的对话历史，支持工具调用结果的传递
2. **自动上下文窗口管理**：trim_messages_history 自动压缩，保证不超出模型限制
3. **自我进化机制的依赖**：工作记忆、长期记忆、记忆压缩都依赖它

### 当前问题的根源

**不是"有三层 history"**，而是：
- **NiuRunner 没有正确初始化 BaseSession.history**
- **会话恢复时没有同步数据库 history 到 BaseSession.history**

### 正确的修复方向

**保留 BaseSession.history**，但：
1. 添加 `BaseSession.load_history()` 方法，支持从外部加载历史
2. 在 NiuRunner.chat() 中，会话开始时加载历史到 BaseSession
3. 调整 agent_runner_loop，不再处理 history 参数（或仅用于调试）

### 关键洞察

**BaseSession.history 不是"冗余层"**，它是 GenericAgent 的**核心组件**，负责：
- 对话状态管理
- 上下文窗口自动管理
- 多轮工具调用的上下文传递

**正确的架构是"三层协作"**：
1. **数据库 history**：持久化、前端展示
2. **BaseSession.history**：LLM 调用、上下文管理
3. **agent_runner_loop 的 history 参数**：仅用于初始化（或完全移除）

---

## 10. 后续行动建议

1. **立即修复**：实现方案 B，添加 `BaseSession.load_history()` 方法
2. **测试验证**：
   - 测试多轮工具调用的上下文传递
   - 测试会话恢复（刷新页面后继续对话）
   - 测试上下文窗口自动压缩
3. **架构优化**（可选）：
   - 移除 agent_runner_loop 的 history 参数（或标记为 deprecated）
   - 添加日志，追踪 history 的加载和更新
   - 考虑在 NiuRunner 中添加 history 同步机制（定期保存 BaseSession.history 到数据库）
