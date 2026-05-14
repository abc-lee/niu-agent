# 双管道架构方案：解决消息持久化与显示的语义混淆

> 版本：3.0 | 日期：2026-05-14 | 状态：待实施 | 修订：取消"三重丢弃"，改为全量工具输出

---

## 1. 问题根因分析

### 1.1 架构缺陷，不是代码bug

当前系统的核心问题不是某个函数写错了，而是**数据流架构缺少语义分层**。整个系统只有一条管道——`agent_runner_loop` 的 yield 生成器——同时承担了三个完全不同的职责：

| 职责 | 需要的数据 | 当前获得的数据 |
|------|-----------|--------------|
| 前端显示 | LLM纯文本回复 | LLM文本 + 调试标记 + 工具调用标记 + 代码围栏 |
| 数据库持久化 | 完整对话（含tool_calls/tool_results） | 只有LLM纯文本，工具数据丢失 |
| LLM上下文 | 完整对话（含tool消息，保证上下文连贯） | 只有role+content，工具输出丢失 |

**根因**：yield 管道是"语义单通道"——5种不同语义的内容挤在同一根管子里，下游无法区分。

### 1.2 "三重丢弃"的来龙去脉与错误

"三重丢弃"不是设计，是补丁的补丁：

1. **不持久化**：`add_message()` 的调用方从未传入 `tool_calls`/`tool_results`，因为调用方拿不到这些数据——它们只存在于 `agent_runner_loop` 的局部变量 `messages` 中
2. **不加载**：`load_history()` 只取 `{role, content}`，因为即使加载了 `tool_calls`/`tool_results`，`agent_loop` 也不接受 `role="tool"` 的消息
3. **不注入**：`agent_loop` 只接受 `user/assistant` 角色（行106-112：`if role in ("user", "assistant") and content`），tool消息被直接丢弃

**v2方案的错误**：v2保留了"三重丢弃"策略，认为"LLM上下文不需要工具细节"。这是错误的：

- **LLM必须看到工具输出**：当LLM发现需要使用技能时，它去读skills，读skills的结果就是工具输出。如果load_history不返回工具输出，那LLM在新对话中就看不到之前读过的skills内容，会重复调用工具。
- **上下文连贯性**：assistant消息中的`tool_calls`字段和后续的`role="tool"`消息是成对的。如果只保留assistant的content而丢弃tool_calls和tool消息，LLM看到的对话是断裂的——assistant说"我来调用xxx工具"，但LLM看不到调用结果，无法理解后续对话的上下文。
- **重复工具调用**：LLM看不到之前的工具输出，会在新对话中重复执行相同的工具调用，浪费token和时间。

**v3修正**：取消"三重丢弃"，改为"完整还原"：
- 持久化：add_message存tool_calls和tool_results
- 加载：load_history返回完整消息序列（含tool_calls、tool_call_id、role="tool"）
- 注入：agent_loop接受所有角色（user/assistant/tool），完整还原对话上下文

### 1.3 Bug 1 的根因：不该存的存了

`verbose=True` 路径下，`agent_runner_loop` yield 的内容包括：

```python
# 5种不同语义的yield混合在一起
yield "\n[LLM Running...]\n"                    # 调试标记
yield chunk                                      # LLM流式回复
yield f"\n[Tool Call: {name}({args})]\n"        # 工具调用标记
yield f"```{lang}\n{code}\n```\n"               # 代码块围栏
yield "---\n"                                    # 格式分隔符
```

`runner.py` 的 `chat()` 用 `verbose=False` 调用，理论上过滤了标记。但 `_clean_stream_output()` 的存在说明：**即使 verbose=False，仍有泄漏**。这是因为 LLM 本身可能输出 `<tool_use>` 等结构化标签，而 `_clean_stream_output` 是用正则后清理来补这个洞的。

**根因**：yield 管道没有类型标记，下游无法区分"LLM回复文本"和"系统注入的标记"。

### 1.4 Bug 2 的根因：该存的没存

工具调用和工具结果的数据流：

```
agent_runner_loop:
  messages = [...]                    # 内存中有完整tool_calls/tool_results
  for turn in loop:
      response = llm.chat(messages)   # response包含tool_calls
      messages.append(response)        # 内存中有
      if tool_calls:
          results = handler.dispatch() # 执行工具
          messages.append(tool_result) # 内存中有
  return {"result": text, "data": ...} # 函数返回后messages丢失
                                        # 没有任何路径把tool数据传给调用方
```

`runner.py` 的 `chat()` 方法：

```python
for chunk in agent_runner_loop(...):
    full_resp += chunk               # 只累积yield的文本
# full_resp中没有tool_calls/tool_results
# add_message(content=full_resp)     # 只存了文本
```

**根因**：`agent_runner_loop` 的 return value 只返回最终文本和 `outcome.data`，不返回中间的 `tool_calls`/`tool_results`。yield 管道也从未传递这些结构化数据。

---

## 2. 架构设计：双管道分离

### 2.1 核心思想

将当前的"单yield管道"拆分为两条独立管道：

```
                    ┌─────────────────────────────────┐
                    │       agent_runner_loop          │
                    │                                   │
  user_msg ──────► │  内部 messages 列表（完整数据）    │
                    │                                   │
                    │  ┌─────────────┐ ┌─────────────┐ │
                    │  │ SSE管道      │ │ DB管道       │ │
                    │  │ (yield)     │ │ (return)    │ │
                    │  └──────┬──────┘ └──────┬──────┘ │
                    └─────────┼───────────────┼────────┘
                              │               │
                    ┌─────────▼───────┐ ┌─────▼──────────┐
                    │  前端显示        │ │  数据库持久化   │
                    │  只看LLM纯文本  │ │  完整对话数据   │
                    └─────────────────┘ └────────────────┘
```

- **SSE管道**（yield）：只传递前端需要显示的内容——LLM纯文本回复
- **DB管道**（return value）：通过生成器的 return value 携带完整 `messages` 列表，由调用方在 async 上下文中批量写入数据库

### 2.2 数据流重构

#### 当前数据流（问题流）

```
agent_runner_loop (yield混合5种内容)
    │
    ▼
runner.chat() (累积full_resp)
    │
    ├─► SSE: yield full_resp.strip()     ← 包含调试标记/结构化标签
    │
    └─► DB: add_message(content=full_resp) ← 只有文本，无tool数据
```

#### 目标数据流（双管道）

```
agent_runner_loop
    │
    ├─ SSE管道 (yield): 只yield StreamEvent(TEXT, content)
    │
    └─ DB管道 (return value): 携带完整 messages 列表
         │
         ▼
    runner.chat()
         │
         ├─► SSE: yield StreamEvent.content → 前端显示
         │
         └─► 生成器结束后，从 return_value 提取 messages
              → 在 async 上下文中批量 add_message
```

### 2.3 设计原则

1. **源头分离**：在 `agent_runner_loop` 内部就区分"显示内容"和"持久化内容"，不靠下游过滤
2. **SSE管道只传显示内容**：yield 的每一项都是前端应该显示的，无需后清理
3. **DB管道通过 return value 传递完整数据**：生成器结束时，return value 携带完整 `messages` 列表（含 tool_calls/tool_results/role="tool"），由调用方在 async 上下文中写入数据库
4. **LLM上下文完整还原**：`load_history()` 返回完整消息序列（含 tool_calls、tool_call_id、role="tool"），`agent_loop` 接受所有角色，保证上下文连贯
5. **SSE管道不送工具输出给前端**：工具输出只存数据库和注入LLM上下文，前端只看主Agent解释后的纯文本对话
6. **同步架构不变**：agent_runner_loop 保持同步生成器，不引入 asyncio

---

## 3. 同步/异步矛盾：核心设计决策

### 3.1 问题描述

`agent_runner_loop` 是同步生成器，`session.py` 的 `add_message()` 是 `async def`（使用 aiosqlite）。v1 方案提出用同步 `db_callback` 在工具调用完成后实时写入 DB，但同步回调无法 `await` 异步函数。

### 3.2 方案比选

#### 方案A：db_callback 中用 asyncio.run_coroutine_threadsafe() 桥接

```python
def _db_persist_callback(self, msg_type, msg_data):
    loop = asyncio.get_event_loop()  # 获取 FastAPI 主循环
    future = asyncio.run_coroutine_threadsafe(
        store.add_message(role=..., content=...),
        loop
    )
    future.result(timeout=5)  # 阻塞等待完成
```

**问题**：
- `chat()` 在工作线程中运行（通过 `asyncio.to_thread` 或 `run_in_executor`），工作线程中 `asyncio.get_event_loop()` 获取的是 FastAPI 主循环，但需要显式传递——容易出错
- 每条消息一次 `run_coroutine_threadsafe` + `.result()`，线程切换开销大，高频调用（每轮2-3条消息 x 40轮 = 80-120次）会显著拖慢 agent 循环
- 如果 DB 写入慢（SQLite 锁竞争），会阻塞 agent 循环，影响 LLM 调用延迟
- 项目中已有 `call_async` 模式（lightrag_manager.py），但它使用专用守护循环，不适合 DB 写入（DB 需要共享 aiosqlite 连接上下文）

#### 方案B：db_callback 中用 sqlite3 同步写入

```python
def _db_persist_callback(self, msg_type, msg_data):
    import sqlite3
    conn = sqlite3.connect(self.db_path)
    conn.execute("INSERT INTO messages ...", (...))
    conn.commit()
    conn.close()
```

**问题**：
- 绕过 aiosqlite，直接用 sqlite3，破坏了统一的数据库访问层
- 每次回调都打开/关闭连接，性能差
- 与 aiosqlite 的连接池/事务管理冲突，可能导致锁竞争
- 未来如果切换到 PostgreSQL 等不支持同步访问的数据库，此方案不可行

#### 方案C（选择）：return value 携带完整 messages 列表，调用方在 async 上下文中批量写入

```python
# agent_runner_loop 的 return value 扩展
return {
    "result": final_text,
    "data": outcome.data,
    "messages": messages,  # 新增：完整消息列表
}

# runner.py 的 chat() 结束后
return_value = gen.return_value  # 同步获取
# ... yield 给前端 ...

# 调用方（compat.py / chat.py）在 async 上下文中批量写入
if return_value and "messages" in return_value:
    for msg in return_value["messages"]:
        await store.add_message(
            role=msg["role"],
            content=msg.get("content", ""),
            tool_calls=msg.get("tool_calls"),
            tool_results=msg.get("tool_results"),
            tool_call_id=msg.get("tool_call_id"),
        )
```

**优势**：
- **零同步/异步桥接**：agent_runner_loop 保持纯同步，DB 写入在 async 上下文中完成
- **零额外线程切换**：不需要 `run_coroutine_threadsafe`，不需要守护循环
- **批量写入更高效**：一次对话结束后批量写入，比逐条回调少 N 次线程切换
- **架构最简洁**：不引入新的桥接机制，复用现有的 return value 通道
- **调用方已有 async 上下文**：`compat.py` 的 `chat_session()` 和 `chat.py` 的 `chat_sync()` 本身就是 async def，天然可以 `await store.add_message()`
- **中途崩溃数据丢失可接受**：agent_runner_loop 崩溃时，messages 在内存中丢失。但崩溃是异常场景，且 LLM 上下文（messages 列表）本身也会丢失。崩溃恢复是另一个问题，不应为此引入复杂的实时持久化

**权衡**：
- 非实时：对话进行中 DB 没有数据，只在对话结束后批量写入。但这对当前系统没有影响——当前系统本来就不在对话中写入 tool 数据
- 内存占用：messages 列表在对话期间全部在内存中。但当前就是这样（agent_runner_loop 的 `messages` 局部变量），没有增加内存占用

### 3.3 方案C 的退出路径完整性

所有退出路径都必须确保 return value 携带完整 messages：

| 退出路径 | 当前 return value | 目标 return value |
|----------|------------------|------------------|
| 正常结束（无工具调用） | `should_exit` 或 `None` | `{"result": ..., "data": ..., "messages": messages}` |
| should_exit | `{"result": "EXITED", "data": ...}` | `{"result": "EXITED", "data": ..., "messages": messages}` |
| CONTEXT_OVERFLOW | `{"result": "CONTEXT_OVERFLOW", "data": {...}}` | `{"result": "CONTEXT_OVERFLOW", "data": {...}, "messages": messages}` |
| MAX_TURNS_EXCEEDED | `{"result": "MAX_TURNS_EXCEEDED"}` | `{"result": "MAX_TURNS_EXCEEDED", "messages": messages}` |
| next_prompt 为空 | `should_exit` | `{"result": ..., "data": ..., "messages": messages}` |

**关键**：`messages` 列表包含到退出时刻为止的所有消息，包括最后一轮的 tool_calls 和 tool_results。调用方可以安全地批量写入。

---

## 4. 具体改动清单

### 4.1 定义消息类型枚举

**文件**：`agent/generic/agent_loop.py`（新增类型定义）

**为什么**：yield 管道需要类型标记，让下游知道每个 yield 项的语义。这是"源头分离"的基础。

**怎么改**：在文件顶部新增枚举和命名元组

```python
from enum import Enum
from typing import NamedTuple, Optional, Any

class StreamEventType(Enum):
    """SSE管道的事件类型——只有前端需要显示的内容"""
    TEXT = "text"           # LLM纯文本回复
    TOOL_STATUS = "tool_status"  # 工具状态通知（前端显示"正在调用xxx"）

class StreamEvent(NamedTuple):
    """SSE管道的yield项"""
    type: StreamEventType
    content: str

    def __str__(self):
        """兼容：str(event) == event.content，避免下游 TypeError"""
        return self.content
```

**设计说明**：
- `TOOL_STATUS` 类型保留工具状态通知能力（替代 verbose=True 路径下的工具调用标记），前端可选择显示或忽略
- `StreamEvent.__str__` 返回 `content`，确保 `str(event)` 兼容现有字符串拼接代码

### 4.2 重构 agent_runner_loop

**文件**：`agent/generic/agent_loop.py`

**为什么**：这是根因所在——当前 yield 混合了5种语义，且 tool 数据没有出口。同时 history 注入逻辑（行106-112）只接受 `user/assistant` 角色，需要扩展支持 `tool` 角色。

**怎么改**：

#### 4.2.1 函数签名变更

```python
# 当前
def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema,
                      max_turns=40, verbose=True, initial_user_content=None,
                      history=None, on_turn_end=None,
                      context_window_tokens=0, context_fifo_threshold=0):

# 目标
def agent_runner_loop(client, system_prompt, user_input, handler, tools_schema,
                      max_turns=40, initial_user_content=None,
                      history=None, on_turn_end=None,
                      context_window_tokens=0, context_fifo_threshold=0,
                      verbose=True,  # 保留但废弃：打印 DeprecationWarning 后忽略
                      ):
```

- **保留 `verbose` 参数**（向后兼容），但打印 `DeprecationWarning` 并忽略其值
- **不新增 `db_callback` 参数**——DB 管道通过 return value 传递，不需要回调

#### 4.2.2 history 注入：支持完整消息序列（v3关键改动）

**当前代码**（行106-112）：

```python
if history:
    for msg in history:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
```

**问题**：
1. 只接受 `user/assistant` 角色，`tool` 消息被丢弃
2. 只取 `content` 字段，`tool_calls`、`tool_call_id` 等字段被丢弃
3. `content` 为空时整条消息被跳过——但 `assistant` 消息可能只有 `tool_calls` 没有 `content`（LLM调用工具时不产生文本）

**目标代码**：

```python
if history:
    for msg in history:
        role = msg.get("role", "user")
        if role == "tool":
            # tool消息：必须保留 tool_call_id 和 content
            messages.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": msg.get("content", ""),
            })
        elif role == "assistant":
            # assistant消息：保留 content 和 tool_calls
            entry = {"role": "assistant", "content": msg.get("content", "")}
            if msg.get("tool_calls"):
                entry["tool_calls"] = msg["tool_calls"]
            messages.append(entry)
        elif role == "user" and msg.get("content"):
            # user消息：保留 content
            messages.append({"role": "user", "content": msg["content"]})
        # system消息不注入（已在messages[0]中）
```

**关键设计**：
- `role="tool"` 的消息**必须**保留，否则 LLM 看不到工具输出，无法理解上下文
- `assistant` 消息即使 `content` 为空，只要有 `tool_calls` 也必须保留——这是 LLM 调用工具时的标准格式
- `tool_call_id` 是关联 assistant.tool_calls 和 tool 消息的纽带，必须保留
- 消息顺序必须与原始对话一致：user → assistant(tool_calls) → tool(tool_call_id) → assistant → ...

#### 4.2.3 SSE管道：只 yield StreamEvent

替换所有 yield 语句：

```python
# 当前（verbose=True路径）
yield f"**LLM Running (Turn {turn}) ...**\n\n"   # 删除：调试标记不送前端
yield chunk                                       # 修改：包装为StreamEvent
yield f"🛠️ **正在调用工具:** `{tool_name}`..."    # 修改：包装为StreamEvent(TOOL_STATUS)
yield "`````\n"                                    # 删除：格式围栏不送前端

# 目标
yield StreamEvent(type=StreamEventType.TEXT, content=chunk)
yield StreamEvent(type=StreamEventType.TOOL_STATUS,
                  content=f"正在调用: {tool_name}")
```

```python
# 当前（verbose=False路径）
content = response.content or ""
content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)
yield content

# 目标（统一路径，不再区分verbose）
content = response.content or ""
content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)
yield StreamEvent(type=StreamEventType.TEXT, content=content)
```

#### 4.2.4 DB管道：return value 携带完整 messages

在所有 return 语句中，将 `messages` 列表附加到 return value：

```python
# 当前（正常结束）
return should_exit  # should_exit = {"result": "CURRENT_TASK_DONE", "data": ...}

# 目标
if should_exit and isinstance(should_exit, dict):
    should_exit["messages"] = messages
return should_exit

# 当前（CONTEXT_OVERFLOW）
return {"result": "CONTEXT_OVERFLOW", "data": {...}}

# 目标
return {"result": "CONTEXT_OVERFLOW", "data": {...}, "messages": messages}

# 当前（should_exit）
return {"result": "EXITED", "data": outcome.data}

# 目标
return {"result": "EXITED", "data": outcome.data, "messages": messages}

# 当前（MAX_TURNS_EXCEEDED）
return {"result": "MAX_TURNS_EXCEEDED"}

# 目标
return {"result": "MAX_TURNS_EXCEEDED", "messages": messages}

# 当前（next_prompt 为空，正常结束）
return should_exit  # 可能为 None

# 目标
result = should_exit or {}
if isinstance(result, dict):
    result["messages"] = messages
else:
    result = {"result": result, "messages": messages}
return result
```

#### 4.2.5 handler.dispatch() 的 yield 兼容性

**当前问题**：`handler.dispatch()` 内部有 yield 语句（"未知工具"提示、MCP 错误提示等），`agent_runner_loop` 通过 `yield from gen` 透传这些 yield。如果 yield 类型从 `str` 改为 `StreamEvent`，这些地方都需要修改。

**解决方案**：handler.dispatch() 的 yield 继续使用 `StreamEvent` 包装：

```python
# 当前（BaseHandler.dispatch）
yield f"未知工具: {tool_name}\n"

# 目标
yield StreamEvent(type=StreamEventType.TOOL_STATUS,
                  content=f"未知工具: {tool_name}")

# 当前（NiuHandler.dispatch - MCP 错误）
yield f"[MCP Error] Tool not found: {tool_name}\n"

# 目标
yield StreamEvent(type=StreamEventType.TOOL_STATUS,
                  content=f"[MCP Error] Tool not found: {tool_name}")

# 当前（NiuHandler.dispatch - MCP 执行通知）
yield f"[MCP] {tool_name} executed\n"

# 目标
yield StreamEvent(type=StreamEventType.TOOL_STATUS,
                  content=f"[MCP] {tool_name} executed")

# 当前（NiuHandler._call_subagent_gen）
yield f"[SubAgent] Calling {agent_name}...\n"

# 目标
yield StreamEvent(type=StreamEventType.TOOL_STATUS,
                  content=f"[SubAgent] Calling {agent_name}...")

# 当前（NiuHandler._call_subagent_gen - 子Agent结果）
yield f"[SubAgent] {agent_name} completed: {result[:200]}...\n"

# 目标
yield StreamEvent(type=StreamEventType.TOOL_STATUS,
                  content=f"[SubAgent] {agent_name} completed: {result[:200]}...")
```

**handler.py 完整改动清单**：

| 位置 | 当前 yield | 目标 yield |
|------|-----------|-----------|
| `BaseHandler.dispatch()` L64 | `yield f"未知工具: {tool_name}\n"` | `yield StreamEvent(TOOL_STATUS, f"未知工具: {tool_name}")` |
| `NiuHandler.dispatch()` L686 | `yield f"[SubAgent] Calling {agent_name}...\n"` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L746 | `yield f"[SubAgent] ✓ Verified task..."` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L748 | `yield f"[SubAgent] ⚠ Warning:..."` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L752 | `yield f"[SubAgent] {agent_name} completed:..."` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L759 | `yield f"[SubAgent] Error: {e}\n"` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L949 | `yield f"[MCP Error] Tool not found:..."` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L968 | `yield f"[MCP] {tool_name} executed\n"` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L997 | `yield f"[MCP Error] {tool_name}: {e}\n"` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L1019 | `yield f"[MCP] {tool_name} executed\n"` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L1035 | `yield f"[MCP Error] {tool_name}: {e}\n"` | `yield StreamEvent(TOOL_STATUS, ...)` |
| `NiuHandler.dispatch()` L1041 | `yield f"Unknown tool: {tool_name}\n"` | `yield StreamEvent(TOOL_STATUS, ...)` |

**注意**：`tool_before_callback` 和 `tool_after_callback` 不 yield（它们不是生成器），不需要修改。

#### 4.2.6 try_call_generator 兼容性

`try_call_generator` 使用 `yield from` 透传生成器的 yield。由于 handler 方法的 yield 现在也改为 `StreamEvent`，透传行为不变——`yield from` 会正确透传 `StreamEvent` 对象。

```python
# try_call_generator 无需修改
def try_call_generator(func, *args, **kwargs):
    ret = func(*args, **kwargs)
    if hasattr(ret, "__iter__") and not isinstance(ret, (str, bytes, dict, list)):
        ret = yield from ret  # 透传 StreamEvent，无需修改
    return ret
```

#### 4.2.7 verbose 参数废弃处理

```python
def agent_runner_loop(..., verbose=True, ...):
    if verbose is not True:  # 有人显式传了 verbose
        import warnings
        warnings.warn(
            "verbose parameter is deprecated and ignored. "
            "SSE pipeline now uses StreamEvent types.",
            DeprecationWarning,
            stacklevel=2,
        )
    # 后续代码不再使用 verbose 变量
```

#### 4.2.8 调试机制保留

移除 verbose 后，调试信息通过以下途径保留：

1. **stderr 日志**：`print(f"[Debug] ...", file=sys.stderr)` 已有，不受影响
2. **StreamEvent.TOOL_STATUS**：工具状态通知（"正在调用xxx"、"MCP执行完成"等）通过 `TOOL_STATUS` 类型 yield，前端可选择显示
3. **loguru logger**：结构化日志不受影响
4. **_clean_stream_output 保留**：作为安全网保留，但只在最终输出时调用一次（而非作为主要清理机制）

### 4.3 重构 runner.py

**文件**：`agent/runner.py`

**为什么**：`chat()` 方法当前从 `full_resp` 提取所有数据，需要改为：SSE管道只累积显示内容，DB管道通过 return value 获取完整数据。

**怎么改**：

#### 4.3.1 chat() 方法重构

```python
def chat(self, session_id, user_input, stream=True, max_turns=40, history=None):
    # ... 初始化（不变）...

    gen = agent_runner_loop(
        client=self.client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=self.handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        # verbose 不传（使用默认值 True，但被忽略）
        initial_user_content=user_input,
        history=history,
        on_turn_end=self._on_turn_end,
        context_window_tokens=context_window_tokens,
    )

    # 累加输出
    full_resp = ""
    return_value = None
    self.last_return_value = None

    while True:
        try:
            event = next(gen)
            # StreamEvent.__str__ 返回 content，兼容字符串拼接
            content = str(event)
            full_resp += content
            # 流式 yield 给前端
            yield content
        except StopIteration as e:
            return_value = e.value
            break

    # 暴露 return_value 给调用方（含 messages 列表）
    self.last_return_value = return_value

    # 如果 full_resp 为空但有返回值数据，使用返回值
    if not full_resp.strip() and return_value:
        # ... 现有的 return_value 数据提取逻辑（不变）...

    # 安全网：清理 LLM 可能输出的结构化标签
    # 注意：这是安全网，不是主要清理机制。
    # 主要机制是 SSE 管道源头只 yield StreamEvent(TEXT, content)
    full_resp = _clean_stream_output(full_resp)

    yield full_resp.strip()
```

**关键变化**：
- `chunk = next(gen)` → `event = next(gen)` + `content = str(event)`
- `return_value` 现在包含 `"messages"` 键，调用方可以从中提取完整数据
- `_clean_stream_output` 保留作为安全网，但源头已纯净，正常情况下不会触发

#### 4.3.2 不需要 _db_persist_callback

由于 DB 管道通过 return value 传递，`runner.py` 不需要新增 `_db_persist_callback` 方法。DB 写入由 API 层（`compat.py` / `chat.py`）在 async 上下文中完成。

### 4.4 重构 session.py

**文件**：`agent/session.py`

**为什么**：当前 `Message` dataclass 缺少 `tool_call_id` 字段，`add_message()` 不处理 `role="tool"` 的消息，数据库 schema 缺少 `tool_call_id` 列。

**怎么改**：

#### 4.4.1 Message dataclass 新增字段

```python
@dataclass
class Message:
    id: str
    role: str  # 'user' | 'assistant' | 'system' | 'tool'  ← 新增 'tool'
    content: str
    tool_calls: List[Dict] = field(default_factory=list)
    tool_results: List[Dict] = field(default_factory=list)
    tool_call_id: Optional[str] = None    # 新增：tool消息的call_id
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)
```

#### 4.4.2 数据库 schema 升级（安全迁移）

```python
async def init_db(self):
    """Initialize database schema"""
    async with aiosqlite.connect(self.db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                content TEXT,
                tool_calls TEXT,
                tool_results TEXT,
                created_at TEXT NOT NULL
            )
        """)

        # 安全迁移：新增 tool_call_id 列（如果不存在）
        # ALTER TABLE ADD COLUMN 在 SQLite 中是安全操作：
        # - 新列默认值为 NULL
        # - 旧数据不受影响
        # - 列已存在时抛出 OperationalError，我们忽略
        try:
            await db.execute("ALTER TABLE messages ADD COLUMN tool_call_id TEXT")
            logger.info("DB migration: added tool_call_id column")
        except Exception:
            pass  # 列已存在，忽略

        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_created_at
            ON messages(created_at ASC)
        """)

        await db.commit()
```

**迁移策略**：
- `ALTER TABLE ADD COLUMN` 是 SQLite 的安全操作，不会锁表或复制数据
- 新列 `tool_call_id` 允许 NULL，旧数据自动为 NULL
- 用 `try/except` 包裹，列已存在时忽略（幂等）
- 不删除旧列，不修改旧列类型

#### 4.4.3 add_message() 支持 role="tool" 和 tool_call_id

```python
async def add_message(
    self,
    role: str,
    content: str,
    tool_calls: List[Dict] = None,
    tool_results: List[Dict] = None,
    tool_call_id: Optional[str] = None,  # 新增参数
) -> str:
    """Add a message"""
    msg_id = str(uuid4())
    created_at = datetime.now().isoformat()
    tool_calls_json = json.dumps(tool_calls or [], ensure_ascii=False)
    tool_results_json = json.dumps(tool_results or [], ensure_ascii=False)

    async with aiosqlite.connect(self.db_path) as db:
        await db.execute(
            """INSERT INTO messages
               (id, role, content, tool_calls, tool_results, tool_call_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (msg_id, role, content, tool_calls_json, tool_results_json,
             tool_call_id, created_at),
        )
        await db.commit()

    logger.debug(f"Added message: {msg_id} role={role}")
    return msg_id
```

#### 4.4.4 get_messages() 返回完整数据

```python
async def get_messages(self, limit=None, before_id=None) -> List[Message]:
    async with aiosqlite.connect(self.db_path) as db:
        db.row_factory = aiosqlite.Row
        # ... 查询逻辑（不变）...

        messages = []
        for row in reversed(rows):
            messages.append(
                Message(
                    id=row["id"],
                    role=row["role"],
                    content=row["content"] or "",
                    tool_calls=json.loads(row["tool_calls"] or "[]"),
                    tool_results=json.loads(row["tool_results"] or "[]"),
                    tool_call_id=row.get("tool_call_id"),  # 新增
                    created_at=row["created_at"],
                )
            )
        return messages
```

### 4.5 重构 context_manager.py（v3关键改动）

**文件**：`agent/context_manager.py`

**为什么**：v2方案保持 context_manager 不变（"三重丢弃"），但这是错误的。LLM必须看到工具输出才能理解上下文连贯性，避免重复工具调用。`load_history()` 必须返回完整消息序列，`compress_messages()` 必须考虑 tool 消息的成对性。

**怎么改**：

#### 4.5.1 load_history() 返回完整消息序列

**当前代码**：

```python
async def load_history(self, limit=None):
    messages = await self.store.get_messages(limit=limit)
    history = []
    for msg in messages:
        if msg.content:  # 跳过空消息
            history.append({
                "role": msg.role,
                "content": msg.content
            })
    return history
```

**问题**：
1. 只返回 `{role, content}`，丢弃了 `tool_calls`、`tool_call_id` 等关键字段
2. `content` 为空时整条消息被跳过——但 `assistant` 消息可能只有 `tool_calls` 没有 `content`
3. 不返回 `role="tool"` 的消息

**目标代码**：

```python
async def load_history(self, limit=None):
    """加载历史消息并转换为 agent_loop 格式

    返回完整消息序列，包括：
    - role="user" 的消息
    - role="assistant" 的消息（含 tool_calls 字段）
    - role="tool" 的消息（含 tool_call_id 字段）

    这保证了 LLM 上下文的连贯性——LLM 能看到工具输出，
    不会在新对话中重复调用相同的工具。
    """
    if limit is None:
        limit = self.max_messages

    messages = await self.store.get_messages(limit=limit)

    history = []
    for msg in messages:
        if msg.role == "tool":
            # tool消息：保留 tool_call_id 和 content
            history.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id or "",
                "content": msg.content or "",
            })
        elif msg.role == "assistant":
            # assistant消息：保留 content 和 tool_calls
            # 注意：assistant消息可能只有tool_calls没有content（LLM调用工具时）
            entry = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
            history.append(entry)
        elif msg.role == "user" and msg.content:
            # user消息：保留 content
            history.append({"role": "user", "content": msg.content})
        # system消息不注入（由agent_loop的system_prompt参数处理）

    return history
```

#### 4.5.2 compress_messages() 成对压缩

**当前代码**：简单的"保留最近80%"策略，不考虑消息间的语义关联。

**问题**：如果压缩时删除了 `assistant(tool_calls)` 消息但保留了对应的 `tool` 消息，LLM API 会报错（tool 消息没有对应的 tool_calls）。反之亦然。

**目标代码**：

```python
def compress_messages(self, messages):
    """压缩消息列表（保留最近消息）

    策略：
    - 保留最近 80% 的消息
    - 删除早期的 20% 消息
    - assistant(tool_calls) + tool(tool_call_id) 必须成对保留或成对删除
    - 如果删除 assistant(tool_calls)，必须连带删除其后的所有 tool 消息
    - 如果删除 tool 消息，必须连带删除其对应的 assistant(tool_calls)

    Args:
        messages: 消息列表

    Returns:
        压缩后的消息列表
    """
    if not messages:
        return messages

    # 计算保留数量
    keep_count = int(len(messages) * 0.8)
    keep_count = max(10, keep_count)

    # 保留最近的消息
    compressed = messages[-keep_count:]

    # 成对性修复：确保压缩后的列表不以孤立的 tool 消息开头
    # 如果 compressed[0] 是 tool 消息，说明它对应的 assistant(tool_calls)
    # 被截断到了删除区域，需要连带删除这个 tool 消息
    while compressed and compressed[0].get("role") == "tool":
        compressed.pop(0)

    # 成对性修复：确保压缩后的列表不以孤立的 assistant(tool_calls) 结尾
    # 如果最后一条是 assistant(tool_calls) 但后面没有对应的 tool 消息，
    # 需要检查原始列表中是否还有后续的 tool 消息
    # （这种情况在"保留最近"策略中不太可能发生，但做防御性检查）
    if compressed and compressed[-1].get("role") == "assistant" and compressed[-1].get("tool_calls"):
        # assistant(tool_calls) 后面应该有 tool 消息
        # 如果原始消息列表中这条 assistant 后面还有 tool 消息，
        # 说明压缩时截断了 tool 消息，需要连带删除这条 assistant
        # 但在"保留最近"策略中，如果 assistant 是最后一条，
        # 它的 tool 消息应该在更后面（即还没产生），所以不需要处理
        pass

    # 如果删除了消息，添加压缩说明
    if len(compressed) < len(messages):
        deleted_count = len(messages) - len(compressed)
        compression_note = {
            "role": "user",
            "content": f"[系统] 为优化性能，已压缩早期 {deleted_count} 条消息。"
        }
        compressed.insert(0, compression_note)

    return compressed
```

**成对性保证的核心逻辑**：
1. 压缩后如果列表开头是 `tool` 消息，说明对应的 `assistant(tool_calls)` 被截断了，必须删除这个 `tool` 消息
2. 压缩后如果列表末尾是 `assistant(tool_calls)` 但没有后续 `tool` 消息，这是合法的（工具还在执行中），不需要处理
3. `user` 消息和纯文本 `assistant` 消息（无 tool_calls）不受成对性约束，可以独立保留或删除

#### 4.5.3 count_tokens_simple() 支持 tool 消息

**当前代码**：只计算 `content` 字段的 token。

**问题**：`tool_calls` 和 `tool_call_id` 也占用 token，但当前不计算。

**目标代码**：

```python
def count_tokens_simple(self, messages):
    """使用 litellm.token_counter 计算 token 数量

    litellm.token_counter 原生支持 tool_calls 和 tool 消息格式，
    可以准确计算包含工具调用的消息的 token 数量。
    """
    try:
        from litellm import token_counter
        return token_counter(model="gpt-4o", messages=messages)
    except Exception:
        # 回退：约 2 字符/token（偏保守，避免低估导致不触发压缩）
        total_tokens = 0
        for msg in messages:
            content = msg.get("content", "")
            total_tokens += max(1, len(content) // 2) + 4
            # tool_calls 额外估算
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    total_tokens += max(1, len(fn.get("arguments", "")) // 2) + 10
        return total_tokens
```

#### 4.5.4 get_context_for_chat() 更新

```python
async def get_context_for_chat(self, exclude_last=True):
    """获取用于聊天的上下文（主入口）

    流程：
    1. 加载历史消息（含 tool 消息）
    2. 检查是否需要压缩
    3. 如果需要，执行压缩（保证成对性）
    4. 返回最终消息列表
    """
    history = await self.load_history()

    if exclude_last and history:
        # 排除最后一条 user 消息（当前用户输入）
        # 注意：如果最后一条是 tool 消息，不应该排除
        if history[-1].get("role") == "user":
            history = history[:-1]

    if self.should_compress(history):
        history = self.compress_messages(history)

    return history
```

### 4.6 重构 API 层：批量写入 DB

#### 4.6.1 新增辅助函数：从 messages 列表批量写入 DB

**文件**：`agent/session.py`（或 `niu_api/chat.py`，放在 session.py 更合适）

```python
async def persist_messages(store: MessageStore, messages: list[dict]) -> list[str]:
    """将 agent_runner_loop 的 messages 列表批量写入数据库

    Args:
        store: MessageStore 实例
        messages: agent_runner_loop 返回的完整消息列表

    Returns:
        写入的消息 ID 列表
    """
    msg_ids = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue  # system 消息不持久化

        content = msg.get("content", "") or ""
        tool_calls = msg.get("tool_calls")
        tool_call_id = msg.get("tool_call_id")

        # 构造 tool_results：从 role="tool" 的消息中提取
        tool_results = None
        if role == "tool" and tool_call_id:
            tool_results = [{"tool_call_id": tool_call_id, "content": content}]

        msg_id = await store.add_message(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_results=tool_results,
            tool_call_id=tool_call_id,
        )
        msg_ids.append(msg_id)

    return msg_ids
```

#### 4.6.2 重构 compat.py 的 chat_session()

```python
@router.post("/api/chat/session")
async def chat_session(request: ChatRequest):
    # ... 初始化、锁、user消息持久化（不变）...

    def sync_chat():
        chunks = []
        for chunk in runner.chat(session_id, request.message, stream=False, history=history_for_runner):
            chunks.append(chunk)
        return "".join(chunks)

    full_reply = await asyncio.to_thread(sync_chat)

    # === DB管道：从 return_value 批量写入完整消息 ===
    rv = getattr(runner, "last_return_value", None)
    if rv and isinstance(rv, dict) and "messages" in rv:
        from agent.session import persist_messages
        await persist_messages(store, rv["messages"])
    else:
        # 回退：return_value 没有 messages，只存 assistant 文本
        if full_reply.strip():
            await store.add_message(role="assistant", content=full_reply)

    # ... 后续处理（溢出检测、auto_tidy等，不变）...
```

#### 4.6.3 重构 chat.py 的 chat_sync()

```python
@router.post("/chat/sync")
async def chat_sync(request: ChatRequest):
    # ... 初始化、锁、user消息持久化（不变）...

    def sync_chat():
        full_reply = ""
        for chunk in runner.chat(session_id, request.message, stream=True, history=history_for_runner):
            full_reply += chunk
        return full_reply

    full_reply = await loop.run_in_executor(None, sync_chat)

    # === DB管道：从 return_value 批量写入完整消息 ===
    rv = getattr(runner, "last_return_value", None)
    if rv and isinstance(rv, dict) and "messages" in rv:
        from agent.session import persist_messages
        await persist_messages(store, rv["messages"])
    else:
        # 回退
        full_reply = _clean_stream_output(full_reply)
        if full_reply.strip():
            await store.add_message(role="assistant", content=full_reply)

    # ... 后续处理（不变）...
```

#### 4.6.4 重构 chat.py 的 SSE 端点

SSE 端点的 `sync_stream()` 在工作线程中运行，生成器结束后 `runner.last_return_value` 已设置。SSE 端点在 async 上下文中可以访问它：

```python
async def generate():
    # ... 流式输出（不变）...

    # 生成器结束后，批量写入 DB
    rv = getattr(runner, "last_return_value", None)
    if rv and isinstance(rv, dict) and "messages" in rv:
        from agent.session import persist_messages
        store = await get_message_store()
        await persist_messages(store, rv["messages"])

    # ... 后续处理（溢出检测、auto_tidy等，不变）...
```

### 4.7 重构 subagent.py

**文件**：`agent/subagent.py`

**为什么**：子 Agent 也调用 `agent_runner_loop`，需要同步修改以处理 `StreamEvent` yield 和 return value 中的 `messages`。

**怎么改**：

#### 4.7.1 _run_agent_loop 适配 StreamEvent

```python
def _run_agent_loop(agent_name, client, system_prompt, user_input,
                    handler, tools_schema, max_turns=20, ...):
    from .generic.agent_loop import agent_runner_loop, StreamEvent

    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        # verbose 不传（使用默认值，被忽略）
        initial_user_content=initial_user_content,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=context_fifo_threshold,
    )

    result = ""
    return_value = None

    while True:
        try:
            event = next(gen)
            # StreamEvent.__str__ 返回 content，兼容
            content = str(event)
            if content:
                result += content
        except StopIteration as e:
            return_value = e.value
            break

    return result, return_value
```

**关键变化**：
- `chunk = next(gen)` → `event = next(gen)` + `content = str(event)`
- `isinstance(chunk, str)` 检查不再需要（`str(event)` 总是返回 str）
- `verbose=False` 参数移除（使用默认值，被忽略）
- return_value 现在可能包含 `"messages"` 键，但子 Agent 不需要持久化（子 Agent 是临时 session，干完就消失），所以忽略即可

#### 4.7.2 子 Agent 不需要 DB 持久化

子 Agent 的对话不需要写入主 Agent 的数据库——它们是独立的临时 session。`_run_agent_loop` 的 return value 中的 `"messages"` 键对子 Agent 无用，直接忽略。

---

## 5. 改动顺序

按依赖关系排序，先改底层再改上层。每一步完成后系统应保持可运行状态。

### Phase 1：数据层（无行为变化）

| 步骤 | 文件 | 改动 | 验证 |
|------|------|------|------|
| 1.1 | `agent/session.py` | Message 新增 `tool_call_id` 字段，role 支持 `"tool"` | Python import 测试 |
| 1.2 | `agent/session.py` | DB schema 新增 `tool_call_id` 列（ALTER TABLE） | 手动检查 messages.db schema |
| 1.3 | `agent/session.py` | `add_message()` 接受 `tool_call_id` 参数 | 单元测试：存取 role="tool" 消息 |
| 1.4 | `agent/session.py` | `get_messages()` 返回 `tool_call_id` | 单元测试：读回完整数据 |
| 1.5 | `agent/session.py` | 新增 `persist_messages()` 辅助函数 | 单元测试：批量写入 |

**验证标准**：现有功能不受影响，新增字段为可选，旧数据兼容。

### Phase 2：类型定义（无行为变化）

| 步骤 | 文件 | 改动 | 验证 |
|------|------|------|------|
| 2.1 | `agent/generic/agent_loop.py` | 新增 `StreamEventType`、`StreamEvent` 枚举 | Python import 测试 |
| 2.2 | `agent/generic/agent_loop.py` | 验证 `StreamEvent.__str__` 兼容性 | `assert str(StreamEvent(TEXT, "hello")) == "hello"` |

**验证标准**：枚举可正常导入，不影响任何现有代码。

### Phase 3：agent_loop 重构（核心改动）

| 步骤 | 文件 | 改动 | 验证 |
|------|------|------|------|
| 3.1 | `agent/generic/agent_loop.py` | verbose 参数废弃处理（DeprecationWarning） | 传 verbose=False 时打印警告 |
| 3.2 | `agent/generic/agent_loop.py` | history 注入逻辑扩展：支持 role="tool"、tool_calls、tool_call_id | 单元测试：含tool消息的history注入 |
| 3.3 | `agent/generic/agent_loop.py` | SSE管道：yield 改为 `StreamEvent` | 调用方需要处理 StreamEvent |
| 3.4 | `agent/generic/agent_loop.py` | DB管道：所有 return 语句附加 `"messages": messages` | 验证return_value包含messages |
| 3.5 | `agent/handler.py` | dispatch() 所有 yield 改为 `StreamEvent` | 未知工具/MCP错误路径测试 |

**验证标准**：
- 不传 `db_callback` 时，系统行为与当前完全一致（除了 yield 类型从 str 变为 StreamEvent）
- return value 包含 `"messages"` 键，内容为完整消息列表
- yield 的内容只包含 LLM 纯文本和工具状态通知，无调试标记
- history 注入支持 tool 消息，LLM 上下文连贯

### Phase 4：context_manager 重构（v3关键改动）

| 步骤 | 文件 | 改动 | 验证 |
|------|------|------|------|
| 4.1 | `agent/context_manager.py` | `load_history()` 返回完整消息序列（含tool_calls、tool_call_id、role="tool"） | 单元测试：含tool消息的history加载 |
| 4.2 | `agent/context_manager.py` | `compress_messages()` 成对压缩（assistant(tool_calls)+tool成对保留/删除） | 单元测试：压缩后无孤立tool消息 |
| 4.3 | `agent/context_manager.py` | `count_tokens_simple()` 支持 tool_calls 字段 | 单元测试：token计数包含tool_calls |
| 4.4 | `agent/context_manager.py` | `get_context_for_chat()` 更新 | 集成测试：新对话LLM能看到工具输出 |

**验证标准**：
- `load_history()` 返回的消息包含 tool_calls、tool_call_id、role="tool"
- 压缩后不会出现孤立的 tool 消息（没有对应 assistant(tool_calls)）
- 压缩后不会出现孤立的 assistant(tool_calls)（没有对应 tool 消息）
- LLM 在新对话中能看到之前的工具输出，不会重复调用工具

### Phase 5：调用方适配

| 步骤 | 文件 | 改动 | 验证 |
|------|------|------|------|
| 5.1 | `agent/runner.py` | `chat()` 处理 `StreamEvent` yield（`str(event)`） | 集成测试：验证SSE输出 |
| 5.2 | `agent/runner.py` | `chat()` 不再从 full_resp 做持久化 | 验证：chat() 不调用 add_message |
| 5.3 | `agent/subagent.py` | `_run_agent_loop()` 处理 `StreamEvent` yield | 子Agent调用测试 |
| 5.4 | `agent/subagent.py` | 移除 `verbose=False` 参数 | Python 语法检查 |

**验证标准**：
- `chat()` yield 的内容是纯文本，无调试标记
- 子 Agent 调用正常（chat-with-xxx）

### Phase 6：API层适配（DB写入）

| 步骤 | 文件 | 改动 | 验证 |
|------|------|------|------|
| 6.1 | `niu_api/compat.py` | `chat_session()` 从 return_value 批量写入 DB | 端到端测试 |
| 6.2 | `niu_api/chat.py` | `chat_sync()` 从 return_value 批量写入 DB | 端到端测试 |
| 6.3 | `niu_api/chat.py` | SSE 端点从 return_value 批量写入 DB | 端到端测试 |

**验证标准**：
- 对话后 DB 中有完整的 tool 数据
- 前端显示无变化（纯文本对话）
- LLM 上下文包含完整工具输出（load_history 返回含 tool 消息的完整序列）

---

## 6. 风险评估

### 6.1 高风险改动

#### agent_loop.py 的 yield 类型变更

**风险**：所有消费 `agent_runner_loop` yield 的代码都假设 yield 的是 `str`。改为 `StreamEvent` 后，如果遗漏了某个消费方，会导致 `TypeError`。

**影响范围**：
- `runner.py` 的 `chat()` 方法（主要消费方）
- `subagent.py` 的 `_run_agent_loop()`（子Agent消费方）
- 任何直接调用 `agent_runner_loop` 的测试代码

**缓解措施**：
1. `StreamEvent.__str__` 返回 `content`，使 `str(event)` 等价于 `event.content`
2. 在 `runner.chat()` 中使用 `content = str(event)`，兼容 StreamEvent 和纯 str
3. 过渡期支持：如果 yield 了纯 str，`str()` 也不影响

**回退方案**：如果 StreamEvent 导致兼容性问题，可以在 agent_loop 内部只 yield str，但用特殊前缀标记类型（如 `\x01TEXT:` 和 `\x02CODE:`），下游按前缀解析。这不如 StreamEvent 优雅，但更安全。

#### handler.py 的 yield 类型变更

**风险**：`handler.dispatch()` 有 12 处 yield 语句需要修改，遗漏任何一处都会导致类型不一致。

**影响范围**：所有工具调用路径（MCP工具、子Agent、未知工具）。

**缓解措施**：
1. 完整改动清单已在 4.2.5 节列出，逐项核对
2. `try_call_generator` 无需修改（`yield from` 透传 StreamEvent）
3. `tool_before_callback` 和 `tool_after_callback` 不 yield，不需要修改

**回退方案**：如果遗漏导致 TypeError，StreamEvent.__str__ 兜底——`str(event)` 返回 content，不会崩溃。

#### context_manager.py 的 load_history 返回 tool 消息（v3新增风险）

**风险**：tool 消息（特别是大文件的解析结果）可能导致上下文溢出。一次工具调用可能返回数千 token 的内容，多条 tool 消息累积后可能超出上下文窗口。

**影响范围**：所有使用 `load_history()` 的对话——新对话启动时加载历史，如果历史中包含大量 tool 消息，可能导致 token 超限。

**缓解措施**：
1. `compress_messages()` 的成对压缩逻辑确保在压缩时正确处理 tool 消息
2. `should_compress()` 的 token 计数现在包含 tool_calls，能更准确地检测溢出风险
3. agent_loop 已有 `context_window_tokens` 参数和 FIFO 截断机制（行274-304），作为运行时保护
4. **已知限制+后续改进**：当前先全量输出 tool 消息，后续增加 tool 输出截断机制（如限制单条 tool 消息最大 token 数，或对旧 tool 消息做摘要替换）

**回退方案**：如果 tool 消息导致频繁溢出，可以在 `load_history()` 中增加 `max_tool_content_tokens` 参数，对超长的 tool 内容做截断（保留前 N 个 token + "...[截断]"）。

### 6.2 中风险改动

#### session.py 的 DB schema 变更

**风险**：ALTER TABLE 在某些 SQLite 版本中可能有限制。新增列的默认值需要处理。

**影响范围**：所有使用 `messages.db` 的代码。

**缓解措施**：
1. `tool_call_id` 列允许 NULL，旧数据自动为 NULL
2. 用 `try/except` 包裹 ALTER TABLE，列已存在时忽略（幂等）
3. 不删除旧列，只新增

**回退方案**：如果 ALTER TABLE 失败，可以新建 `tool_messages` 表存储 tool 相关数据，通过 message_id 关联。

#### return value 格式变更

**风险**：所有消费 `agent_runner_loop` return value 的代码都假设 return value 是 `{"result": ..., "data": ...}` 格式。新增 `"messages"` 键后，如果消费方遍历 dict 的 keys，可能意外处理 messages 数据。

**影响范围**：
- `runner.py` 的 `chat()` 方法
- `subagent.py` 的 `_extract_result_from_return_value()`
- `compat.py` 的溢出检测逻辑

**缓解措施**：
1. `"messages"` 键是新增，不影响现有的 `"result"` 和 `"data"` 键
2. `_extract_result_from_return_value()` 只检查 `return_value.get("result")` 和 `return_value.get("data")`，不遍历 keys
3. 溢出检测只检查 `rv.get("result") == "CONTEXT_OVERFLOW"`，不受影响

**回退方案**：如果消费方有问题，可以将 messages 放在 `"data"` 内部：`{"result": ..., "data": {..., "_messages": messages}}`。

#### compress_messages() 成对性保证

**风险**：成对性逻辑如果实现有误，可能导致 LLM API 报错（tool 消息没有对应的 tool_calls）。

**影响范围**：所有使用压缩功能的对话。

**缓解措施**：
1. 成对性逻辑只在压缩时触发（消息数量 > max_messages 或 token > 80%），非压缩路径不受影响
2. 单元测试覆盖：孤立 tool 消息、孤立 assistant(tool_calls)、正常成对消息
3. agent_loop 的 FIFO 截断机制（行274-304）已有成对性处理逻辑，可参考

**回退方案**：如果成对性逻辑有 bug，可以回退到简单策略——压缩时从删除区域的开头开始，跳过所有 tool 消息，直到遇到 user 消息为止。

### 6.3 低风险改动

#### API层移除手动 add_message

**风险**：如果 `return_value` 没有 `"messages"` 键（例如 agent_runner_loop 异常退出），assistant 消息可能不会被持久化。

**影响范围**：对话历史。

**缓解措施**：
1. 回退逻辑：如果 `return_value` 没有 `"messages"` 键，回退到只存 assistant 文本
2. 在 `persist_messages()` 中添加日志，确认每条消息都被写入
3. 过渡期：双写验证——同时保留手动 `add_message` 和 `persist_messages`，对比两者是否一致

**回退方案**：恢复手动 `add_message`。

#### runner.py 保留 _clean_stream_output

**风险**：如果 agent_loop 的 SSE 管道仍有泄漏（比如 LLM 输出了未预期的标签），删除后清理函数会导致前端显示异常。

**影响范围**：前端显示。

**缓解措施**：
1. 保留 `_clean_stream_output` 作为安全网
2. SSE 管道源头已纯净（只 yield StreamEvent），正常情况下 `_clean_stream_output` 不会触发任何替换
3. 如果观察到 `_clean_stream_output` 长期无替换触发，可以在后续版本中删除

**回退方案**：`_clean_stream_output` 已保留，无需回退。

---

## 7. 验证方法

### 7.1 Phase 1 验证：数据层

```python
# 测试：存取 role="tool" 的消息
store = MessageStore(db_path=":memory:")
await store.init_db()
msg_id = await store.add_message(
    role="tool",
    content="tool result content",
    tool_call_id="call_abc123",
    tool_results=[{"tool_call_id": "call_abc123", "content": "result"}],
)
msgs = await store.get_messages()
assert len(msgs) == 1
assert msgs[0].role == "tool"
assert msgs[0].tool_call_id == "call_abc123"
assert msgs[0].tool_results[0]["content"] == "result"
```

### 7.2 Phase 3 验证：agent_loop 双管道

```python
# 测试：return value 包含完整 messages
gen = agent_runner_loop(
    user_input="搜索Python教程",
    messages=[...],
    handler=mock_handler,
)

# 消费SSE管道
sse_output = ""
for event in gen:
    assert isinstance(event, StreamEvent) or isinstance(event, str)
    sse_output += str(event)

# 验证SSE管道：只有纯文本和工具状态，无调试标记
assert "**LLM Running" not in sse_output
assert "🛠️ **正在调用工具:**" not in sse_output

# 验证DB管道：return value 包含完整 messages
return_value = gen.return_value  # StopIteration.value
assert "messages" in return_value
messages = return_value["messages"]
assistant_msgs = [m for m in messages if m["role"] == "assistant" and m.get("tool_calls")]
tool_msgs = [m for m in messages if m["role"] == "tool"]
assert len(assistant_msgs) > 0, "应该有带tool_calls的assistant消息"
assert len(tool_msgs) > 0, "应该有tool结果消息"
assert tool_msgs[0]["tool_call_id"] is not None, "tool消息应该有tool_call_id"
```

### 7.3 Phase 4 验证：context_manager 完整还原

```python
# 测试：load_history 返回完整消息序列
store = MessageStore(db_path=":memory:")
await store.init_db()

# 模拟一次工具调用的对话
await store.add_message(role="user", content="帮我搜索Python教程")
await store.add_message(role="assistant", content="", tool_calls=[
    {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"query": "Python教程"}'}}
])
await store.add_message(role="tool", content="搜索结果：Python官方教程...", tool_call_id="call_1")
await store.add_message(role="assistant", content="我找到了Python官方教程...")

cm = ContextManager(store)
history = await cm.load_history()

# 验证：完整消息序列
assert len(history) == 4
assert history[0] == {"role": "user", "content": "帮我搜索Python教程"}
assert history[1]["role"] == "assistant"
assert history[1]["tool_calls"] is not None  # tool_calls 被保留
assert history[2]["role"] == "tool"
assert history[2]["tool_call_id"] == "call_1"  # tool_call_id 被保留
assert history[2]["content"] == "搜索结果：Python官方教程..."  # 工具输出被保留
assert history[3] == {"role": "assistant", "content": "我找到了Python官方教程..."}

# 测试：compress_messages 成对性
long_history = history * 20  # 80条消息，触发压缩
compressed = cm.compress_messages(long_history)

# 验证：不会出现孤立的 tool 消息
for i, msg in enumerate(compressed):
    if msg.get("role") == "tool":
        # 前面应该有对应的 assistant(tool_calls)
        assert i > 0, "tool消息不应该出现在列表开头"
        prev = compressed[i-1]
        assert prev.get("role") == "assistant" and prev.get("tool_calls"), \
            f"tool消息前面应该是assistant(tool_calls)，实际是 {prev}"
```

### 7.4 Phase 6 验证：API层集成

```python
# 测试：chat_session 后 DB 中有完整 tool 数据
# 通过 HTTP 请求发送带工具调用的消息
response = await client.post("/api/chat/session", json={
    "message": "搜索Python教程",
    "session_id": "test",
})

# 检查DB
store = await get_message_store()
msgs = await store.get_messages()
tool_call_msgs = [m for m in msgs if m.tool_calls]
tool_result_msgs = [m for m in msgs if m.role == "tool"]

assert len(tool_call_msgs) > 0, "DB中应该有tool_calls数据"
assert len(tool_result_msgs) > 0, "DB中应该有tool结果数据"
```

### 7.5 端到端验证：LLM能看到工具输出

```bash
# 1. 启动完整系统
go run main.go

# 2. 第一轮对话：触发工具调用
# 用户："帮我搜索Python教程"
# LLM调用search工具，得到结果，回复用户

# 3. 第二轮对话（新请求）：验证LLM不重复调用
# 用户："刚才搜索的结果是什么？"
# 预期：LLM能从上下文中看到之前的工具输出，直接回答，不重复调用search

# 4. 验证数据库
sqlite3 ~/.niu/messages.db "SELECT role, tool_calls, tool_results, tool_call_id FROM messages ORDER BY created_at"
# 预期：
# - assistant消息的tool_calls列有实际数据（不再是[]）
# - 有role="tool"的消息行
# - tool消息的tool_call_id列有值

# 5. 验证前端显示
# - 只看到LLM纯文本回复
# - 无调试标记、无工具调用标记
# - 工具输出不直接显示在前端对话中
```

### 7.6 回归测试清单

| 测试项 | 预期结果 |
|--------|---------|
| 纯文本对话（无工具调用） | 行为与当前完全一致 |
| 带工具调用的对话 | 前端只看LLM文本，DB有完整tool数据 |
| 多轮对话 | 每轮的tool数据都持久化 |
| 新对话加载历史 | LLM能看到之前的工具输出，不重复调用工具 |
| 上下文溢出处理 | CONTEXT_OVERFLOW检测仍正常，退出时messages完整写入DB |
| should_exit 退出 | EXITED路径messages完整写入DB |
| MAX_TURNS_EXCEEDED 退出 | 超出轮次时messages完整写入DB |
| 子Agent调用 | chat-with-xxx工具调用正常，子Agent不写主DB |
| 流式SSE | 前端逐字显示，无卡顿 |
| 同步API | /chat/sync返回完整回复 |
| 历史对话加载 | load_history返回完整消息序列（含tool消息） |
| 压缩后成对性 | 压缩后无孤立tool消息或assistant(tool_calls) |
| LightRAG内容提取 | 能从DB读取tool_results数据 |
| 梦境整理 | 能从DB读取完整对话数据 |
| verbose=False 兼容 | 打印DeprecationWarning，行为与verbose=True一致 |
| 大文件工具输出 | 当前全量输出；已知限制，后续增加截断 |

---

## 8. 数据流全景图（改动后）

```
用户输入
    │
    ▼
┌──────────────────────────────────────────────────────┐
│  API层 (chat.py / compat.py)                         │
│  1. await store.add_message(role="user", content=...)│
│  2. 调用 runner.chat(input, session_id)              │
│     （在工作线程中运行，不阻塞事件循环）               │
└──────────────┬───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│  runner.chat()                                        │
│  1. 调用 agent_runner_loop()                         │
│  2. yield str(event) 给SSE端点                       │
│  3. 生成器结束后，return_value 含 messages 列表       │
└──────────────┬───────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────┐
│  agent_runner_loop()                                  │
│                                                       │
│  ┌─────────────────────────────────────────────┐     │
│  │  内部 messages 列表（完整数据）              │     │
│  └──────────┬──────────────────────────────────┘     │
│             │                                        │
│     ┌───────▼───────┐  ┌──────────────────────┐     │
│     │  SSE管道       │  │  DB管道              │     │
│     │  (yield)       │  │  (return value)     │     │
│     │               │  │                      │     │
│     │  只yield:     │  │  生成器结束时:        │     │
│     │  - StreamEvent│  │  return {            │     │
│     │    (TEXT,     │  │    "result": ...,    │     │
│     │     content)  │  │    "data": ...,      │     │
│     │  - StreamEvent│  │    "messages": [     │     │
│     │    (TOOL_     │  │      完整消息列表    │     │
│     │     STATUS,   │  │      含tool_calls,  │     │
│     │     content)  │  │      tool_results,  │     │
│     │               │  │      role="tool"    │     │
│     │  不yield:     │  │    ]                │     │
│     │  - 调试标记   │  │  }                  │     │
│     │  - 格式分隔符 │  │                      │     │
│     │  - 工具输出   │  │                      │     │
│     └───────┬───────┘  └──────┬───────────────┘     │
└─────────────┼─────────────────┼──────────────────────┘
              │                 │
              ▼                 ▼
     ┌────────────────┐ ┌──────────────────────┐
     │  前端显示       │ │  API层 (async)       │
     │  (SSE流)       │ │                      │
     │  只看纯文本    │ │  for msg in          │
     │                │ │    return_value[     │
     │  工具输出不    │ │      "messages"]:    │
     │  直接显示      │ │    await store       │
     │  （由主Agent   │ │      .add_message()  │
     │   解释给用户） │ │                      │
     └────────────────┘ └──────────┬───────────┘
                                   │
                        ┌──────────▼───────────┐
                        │  messages.db          │
                        │                       │
                        │  完整数据：            │
                        │  - assistant消息       │
                        │  - tool_calls列有数据  │
                        │  - role="tool"消息行   │
                        │  - tool_call_id列有值  │
                        └──────────┬───────────┘
                                   │
                        ┌──────────▼───────────┐
                        │  消费者               │
                        │                       │
                        │  LLM上下文            │
                        │  load_history()       │
                        │  → 完整消息序列       │
                        │  → 含tool_calls,     │
                        │    tool_call_id,     │
                        │    role="tool"       │
                        │  → "完整还原"（v3）   │
                        │                       │
                        │  前端显示             │
                        │  → SSE管道纯文本      │
                        │  → 工具输出不显示     │
                        │  → 由主Agent解释      │
                        │                       │
                        │  LightRAG             │
                        │  → 读取tool_results   │
                        │  → 提取知识           │
                        │                       │
                        │  梦境整理             │
                        │  → 读取完整对话       │
                        │  → 压缩/摘要          │
                        └───────────────────────┘
```

---

## 9. 关键设计决策记录

### 决策1：为什么用 return value 而不是 callback？

**选项A**：用 `db_callback` 实时回调。

**问题**：
- `agent_runner_loop` 是同步生成器，`add_message()` 是 `async def`
- 同步回调无法 `await` 异步函数
- 桥接方案（`run_coroutine_threadsafe`）引入线程切换开销，高频调用拖慢 agent 循环
- 需要显式传递事件循环引用，容易出错

**选项B**（选择）：用 return value 携带完整 messages 列表。

**优势**：
- 零同步/异步桥接——agent_runner_loop 保持纯同步
- 零额外线程切换——不需要 `run_coroutine_threadsafe`
- 批量写入更高效——一次对话结束后批量写入，比逐条回调少 N 次线程切换
- 调用方已有 async 上下文——`compat.py` 和 `chat.py` 本身就是 `async def`
- 架构最简洁——不引入新的桥接机制，复用现有的 return value 通道

**权衡**：
- 非实时：对话进行中 DB 没有数据，只在对话结束后批量写入。但当前系统本来就不在对话中写入 tool 数据，这是纯增量
- 中途崩溃数据丢失：可接受——崩溃是异常场景，且 LLM 上下文本身也会丢失

### 决策2：为什么 SSE 管道用 StreamEvent 而不是继续用 str？

**选项A**：继续 yield str，但用特殊前缀标记类型。

**问题**：
- 前缀约定容易出错，下游解析需要正则
- 本质上还是"单管道靠约定区分"，只是换了一种约定

**选项B**（选择）：yield StreamEvent 命名元组。

**优势**：
- 类型安全，下游用 `event.type` 判断，不靠字符串解析
- IDE 可以自动补全和类型检查
- 语义清晰，`StreamEvent(type=TEXT, content="...")` 比 `"\x01TEXT:..."` 更易读
- `__str__` 兼容：`str(event)` 返回 `content`，不会破坏现有字符串拼接

### 决策3：为什么取消"三重丢弃"，改为"完整还原"？（v3关键决策）

**v2方案**：保留"三重丢弃"——`load_history()` 只返回 `{role, content}`，LLM 上下文不包含工具输出。

**v3修正原因**：

1. **LLM必须看到工具输出**：当LLM发现需要使用技能时，它去读skills，读skills的结果就是工具输出。如果load_history不返回工具输出，那LLM在新对话中就看不到之前读过的skills内容，会重复调用工具。

2. **上下文连贯性**：assistant消息中的`tool_calls`和后续的`role="tool"`消息是成对的。如果只保留assistant的content而丢弃tool_calls和tool消息，LLM看到的对话是断裂的——assistant说"我来调用xxx工具"，但LLM看不到调用结果，无法理解后续对话的上下文。

3. **重复工具调用**：LLM看不到之前的工具输出，会在新对话中重复执行相同的工具调用，浪费token和时间。

4. **API规范要求**：OpenAI/Anthropic等LLM API要求tool消息必须与assistant(tool_calls)成对出现。如果注入assistant(tool_calls)但不注入对应的tool消息，API会报错。

**权衡**：
- **上下文溢出风险**：tool消息（特别是大文件解析结果）可能很长，多条tool消息累积后可能超出上下文窗口。但当前先全量输出，截断保护作为后续改进。
- **token消耗增加**：tool消息会占用更多token。但这是必要的代价——没有工具输出，LLM无法理解上下文，会浪费更多token在重复工具调用上。

### 决策4：为什么工具输出不送前端显示？

**原因**：
1. **用户体验**：工具输出通常是结构化数据（JSON、搜索结果、文件内容等），直接显示在前端对话中会干扰用户阅读
2. **语义正确**：工具输出是给LLM看的中间结果，不是给用户看的最终回复。主Agent会基于工具输出生成用户友好的回复
3. **SSE管道职责**：SSE管道只传递前端需要显示的内容——LLM纯文本回复。工具输出通过DB管道持久化，通过load_history注入LLM上下文

### 决策5：为什么保留 verbose 参数而非直接删除？

**原因**：
1. **向后兼容**：`subagent.py` 的 `_run_agent_loop()` 传 `verbose=False`，直接删除会导致 TypeError
2. **渐进式迁移**：先废弃（打印警告），下个版本再删除，给调用方迁移时间
3. **调试路径保留**：verbose=True 的调试信息通过 `StreamEvent.TOOL_STATUS` 和 stderr 日志保留，不丢失调试能力

### 决策6：为什么保留 _clean_stream_output？

**原因**：
1. **安全网**：即使 SSE 管道源头已纯净，LLM 本身可能输出未预期的结构化标签（如 `<tool_use>`、`<text>` 等）
2. **零成本**：如果源头纯净，`_clean_stream_output` 的正则不会匹配任何内容，性能影响为零
3. **可观测性**：如果 `_clean_stream_output` 频繁触发替换，说明源头仍有泄漏，需要修复
4. **后续可删除**：观察一段时间无触发后，可以在后续版本中删除

### 决策7：为什么当前先全量输出工具输出，截断作为后续改进？

**原因**：
1. **优先级**：当前的首要目标是解决"工具数据丢失"问题，让LLM能看到工具输出。截断是优化，不是必须的。
2. **风险控制**：截断逻辑如果实现不当，可能丢失关键信息，导致LLM理解错误。先全量输出，观察实际使用中的token消耗情况，再决定截断策略。
3. **已有保护**：agent_loop 已有 `context_window_tokens` 参数和 FIFO 截断机制（行274-304），作为运行时保护。context_manager 的 `should_compress()` 也能在加载时检测并压缩。
4. **后续改进方向**：
   - 单条 tool 消息截断：限制单条 tool 消息最大 token 数（如 2000 token），超长部分用 "...[截断]" 替代
   - 旧 tool 消息摘要：对超过 N 轮的 tool 消息，用摘要替代原始内容
   - 按工具类型截断：不同工具的输出截断策略不同（如 search 结果保留前3条，file 内容保留前100行）

---

## 10. 过渡策略

### 10.1 渐进式迁移

每个 Phase 完成后系统应保持可运行状态。不建议一次性改完所有文件。

### 10.2 双写验证期

在 Phase 6 移除手动 `add_message` 之前，建议保留双写（手动 + persist_messages）一段时间，对比两者是否一致：

```python
# 过渡期：双写验证
rv = getattr(runner, "last_return_value", None)
if rv and isinstance(rv, dict) and "messages" in rv:
    await persist_messages(store, rv["messages"])

# 手动写入仍然保留，用于对比
if full_reply.strip():
    await store.add_message(role="assistant", content=full_reply)

# 验证：对比 persist_messages 写入和手动写入是否一致
# 如果一致，下一版本移除手动写入
```

### 10.3 verbose 参数废弃计划

1. Phase 3：`verbose` 参数保留但忽略，打印 DeprecationWarning
2. Phase 5：所有调用方不再传 `verbose`（subagent.py 移除 `verbose=False`）
3. 下一个版本：移除 `verbose` 参数

### 10.4 _clean_stream_output 删除计划

1. Phase 5：保留 `_clean_stream_output` 作为安全网
2. 观察期（1-2周）：添加计数器，记录 `_clean_stream_output` 触发替换的次数
3. 如果触发次数为 0：下个版本删除 `_clean_stream_output`
4. 如果触发次数 > 0：分析泄漏来源，修复后再删除

### 10.5 工具输出截断改进计划

1. 当前：全量输出工具输出，不做截断
2. 观察期（1-2周）：统计实际使用中 tool 消息的 token 消耗和上下文溢出频率
3. 如果溢出频繁：实现单条 tool 消息截断（`max_tool_content_tokens` 参数）
4. 后续：实现旧 tool 消息摘要替换

---

## 11. v2 → v3 修订对照表

| 章节 | v2方案 | v3修订 | 修订原因 |
|------|--------|--------|---------|
| 1.2 "三重丢弃" | 认为第3步（不注入tool消息）是正确的 | 取消"三重丢弃"，改为"完整还原" | LLM必须看到工具输出，否则会重复调用工具 |
| 2.3 设计原则4 | LLM上下文保持"三重丢弃" | LLM上下文完整还原，包含tool消息 | 上下文连贯性要求LLM看到工具输出 |
| 4.2.2 history注入 | 只接受user/assistant角色 | 支持tool角色、tool_calls、tool_call_id | agent_loop行106-112需要扩展 |
| 4.5 context_manager | 保持不变 | load_history返回完整消息序列，compress_messages成对压缩 | v2的"保持不变"是错误的 |
| 5. 改动顺序 | 无Phase 4（context_manager） | 新增Phase 4：context_manager重构 | context_manager改动是v3关键改动 |
| 6.1 风险评估 | 无tool消息导致溢出的风险 | 新增：tool消息可能导致上下文溢出 | 全量输出tool消息的已知风险 |
| 7. 验证方法 | 无context_manager验证 | 新增7.3：context_manager完整还原验证 | 验证load_history和compress_messages |
| 8. 数据流全景图 | LLM上下文"三重丢弃（正确）" | LLM上下文"完整还原（v3）" | 消费者行为变化 |
| 9.3 决策3 | 为什么保持"三重丢弃" | 为什么取消"三重丢弃"，改为"完整还原" | 核心决策变更 |
| 9.4 决策4 | 无 | 新增：为什么工具输出不送前端显示 | 明确SSE管道职责 |
| 9.7 决策7 | 无 | 新增：为什么当前先全量输出，截断作为后续改进 | 已知限制+后续改进 |

---

## 12. 已知限制与后续改进

### 12.1 工具输出可能导致上下文溢出

**现状**：当前全量输出工具输出，不做截断。

**风险**：一次工具调用可能返回数千 token 的内容（如文件解析结果、搜索结果），多条 tool 消息累积后可能超出上下文窗口。

**后续改进**：
1. 单条 tool 消息截断：`max_tool_content_tokens` 参数，超长部分用 "...[截断]" 替代
2. 旧 tool 消息摘要：对超过 N 轮的 tool 消息，用摘要替代原始内容
3. 按工具类型截断：不同工具的输出截断策略不同

### 12.2 compress_messages 的成对性保证可能不完善

**现状**：当前只处理了"压缩后列表开头不能是 tool 消息"的情况。

**风险**：更复杂的场景（如连续多个 assistant(tool_calls) + tool 消息对被部分截断）可能需要更精细的处理。

**后续改进**：
1. 更完善的成对性算法：按"消息组"（user + assistant(tool_calls) + tool*）为单位进行压缩
2. 压缩后的完整性校验：遍历压缩后的消息列表，检查每条 tool 消息是否有对应的 assistant(tool_calls)

### 12.3 非实时持久化

**现状**：对话结束后才批量写入 DB，对话进行中 DB 没有数据。

**风险**：agent_runner_loop 异常退出时，整段对话数据丢失。

**后续改进**：
1. 每轮结束后增量持久化：在 `on_turn_end` 回调中写入该轮的 tool 数据
2. 需要解决同步/异步桥接问题（可参考方案C的权衡分析）

---

## 13. 总结

本方案的核心是**双管道分离 + 完整还原**：

1. **SSE管道**（yield StreamEvent）：只传前端需要显示的内容，从源头保证纯净。工具输出不送前端，由主Agent解释给用户。
2. **DB管道**（return value 携带 messages）：传完整对话数据，包括 tool_calls/tool_results，由调用方在 async 上下文中批量写入数据库。
3. **完整还原**（v3关键改动）：`load_history()` 返回完整消息序列（含 tool_calls、tool_call_id、role="tool"），`agent_loop` 接受所有角色，保证 LLM 上下文连贯，避免重复工具调用。

这从根本上解决了两个 bug：
- **Bug 1（不该存的存了）**：SSE管道不再包含调试标记和结构化标签，DB管道只存结构化数据
- **Bug 2（该存的没存）**：DB管道通过 return value 传递完整 messages 列表，数据库中有完整数据

同时取消了"三重丢弃"策略，改为"完整还原"——LLM 上下文包含完整工具输出，保证对话连贯性，避免重复工具调用。

**v3 相比 v2 的关键改进**：
- 取消"三重丢弃"，改为"完整还原"：load_history 返回完整消息序列，agent_loop 支持所有角色
- context_manager.py 重构：load_history 返回含 tool 消息的完整序列，compress_messages 成对压缩
- agent_loop history 注入扩展：支持 role="tool"、tool_calls、tool_call_id
- 明确工具输出不送前端显示：SSE管道只传纯文本，工具输出由主Agent解释
- 工具输出截断作为后续改进：当前先全量输出，标注为已知限制
- 新增决策7：解释为什么当前先全量输出，截断作为后续改进
