# V4 逐条流式推送方案

## 问题陈述

### 问题1：内容重复拼接
- **现象**：最后一条 assistant 消息包含了前面多条消息的内容
- **根因**：`persist_agent_reply` 第178-181行，当 `full_reply`（所有轮次 reply 的拼接）与 `rv["messages"]` 中最后一条 assistant 的 content 不同时，额外写入一条包含整个 `full_reply` 的 assistant 消息。在多轮工具调用场景中，`full_reply` 是所有轮次 reply 的拼接文本，而 `rv["messages"]` 中最后一条 assistant 的 content 只是最后一轮的文本，两者必然不同，导致重复写入。

### 问题2：不会逐条推送
- **现象**：所有内容在 chat 全部完成后一次性推送，中间轮次无任何 DB 写入和 SSE 推送
- **根因**：`persist_agent_reply` 在 `runner.chat()` 全部完成后才调用一次。中间轮次（assistant tool_calls、tool 结果、中间 reply）没有任何 DB 写入和 SSE 推送。前端在整个处理期间只显示 typing 动画。

## 方案核心思路

**复用 `agent_runner_loop` 的 generator 有序流式通道**。

`agent_runner_loop` 已经通过 `yield StreamEvent` 按顺序推送中间状态。问题是 yield 出去的内容不包含完整消息结构（role、tool_calls、tool_call_id 等），无法直接持久化。

**方案**：新增一种 StreamEvent 类型 `"persist"`，让 `agent_runner_loop` 在构建完整消息结构后（append 到 messages 之后），yield 出完整的消息 dict。`runner.chat()` 消费到 `"persist"` 事件时，同步写入 DB + 通知 SSE。

## 详细修改点

---

### 修改1：`agent/generic/agent_loop.py` — 新增 `"persist"` 事件类型 + yield 持久化消息

#### 1a. 扩展 `_VALID_STREAM_TYPES`（第5行）

```python
# 修改前
_VALID_STREAM_TYPES = ("reply", "tool_marker", "system")

# 修改后
_VALID_STREAM_TYPES = ("reply", "tool_marker", "system", "persist")
```

#### 1b. 在 assistant(tool_calls) 消息 append 后 yield persist 事件（第233行后）

在 `messages.append(assistant_msg)` 之后，yield 该消息的持久化指令：

```python
# 第222-233行（现有代码，不修改）
if response.tool_calls:
    assistant_msg = {"role": "assistant", "content": response.content or "", "tool_calls": []}
    for tc in response.tool_calls:
        assistant_msg["tool_calls"].append({
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments
            }
        })
    messages.append(assistant_msg)

# 新增：yield 持久化指令（第233行后插入）
    yield StreamEvent("persist", json.dumps(assistant_msg, ensure_ascii=False))
```

注意：此 yield 在 `if response.tool_calls:` 块内，与 `messages.append(assistant_msg)` 同级缩进。仅在有 tool_calls 时 yield，纯文本回复由最后一轮的 return value 处理。

#### 1c. 在 tool 结果 append 后 yield persist 事件（第293行后）

在 tool 结果 append 循环之后：

```python
# 第288-293行（现有代码，不修改）
for tool_result in tool_results:
    messages.append({
        "role": "tool",
        "tool_call_id": tool_result["tool_use_id"],
        "content": tool_result["content"]
    })

# 新增：yield 每条 tool 结果的持久化指令（第293行后插入）
for tool_result in tool_results:
    tool_msg = {
        "role": "tool",
        "tool_call_id": tool_result["tool_use_id"],
        "content": tool_result["content"]
    }
    yield StreamEvent("persist", json.dumps(tool_msg, ensure_ascii=False))
```

#### 1d. 在纯文本回复（无 tool_calls）时 yield persist 事件

当 LLM 返回纯文本（无 tool_calls），最后一轮的 reply 已经通过 `StreamEvent("reply", content)` yield 了。但这个 reply 没有完整的消息结构。需要在 return 之前 yield 一个 persist 事件。

**关键注意**：纯文本回复时，`messages[-1]` 不是纯文本 assistant 消息（纯文本回复不会 append assistant 消息到 messages 列表），而是 tool 结果或上一轮的 assistant(tool_calls)。因此必须从 `response.content` 构造消息 dict，而不是从 `messages[-1]` 获取。

同时，进入 `if len(next_prompts) == 0` 有两种情况：
1. 没有 tool_calls（`response.tool_calls` 为空/None）→ 纯文本回复，应 yield persist
2. 有 tool_calls 但某次调用的 next_prompt 为空 → 提前退出，不应 yield 纯文本 persist

因此条件必须明确检查 `not response.tool_calls`。

在第296-304行（`if len(next_prompts) == 0:` 分支）中，纯文本回复的 return 路径之前插入：

```python
# 第295-304行（现有代码）
if len(next_prompts) == 0:
    if len(handler._done_hooks) == 0:
        # 纯文本回复：也要执行衰减
        if on_turn_end is not None:
            on_turn_end(messages, tools_schema, turn)
        # 新增：纯文本回复也 yield persist 事件
        # 从 response.content 构造消息（不从 messages[-1] 获取，因为纯文本回复不会 append 到 messages）
        if response.content and not response.tool_calls:
            pure_text_msg = {"role": "assistant", "content": response.content}
            yield StreamEvent("persist", json.dumps(pure_text_msg, ensure_ascii=False))
        if isinstance(should_exit, dict):
            should_exit["messages"] = messages
            return should_exit
        # should_exit 为 None 时（无工具调用），返回标准格式
        return {"result": "CURRENT_TASK_DONE", "data": None, "messages": messages}
```

同理，在第309-316行（`if not next_prompt or not next_prompt.strip():` 分支）中：

**关键注意**：第309行分支和第295行分支的persist逻辑不同。原因：
- 第295行分支（`if len(next_prompts) == 0:`）：在纯文本回复时进入（`response.tool_calls` 为空），此时 `messages[-1]` 不是纯文本assistant消息，需要从 `response.content` 构造纯文本persist。
- 第309行分支（`if not next_prompt or not next_prompt.strip():`）：只在有tool_calls的轮次之后才进入（纯文本回复走第295行分支）。此时 `response.tool_calls` 不为空，`not response.tool_calls` 永远为False，因此1d中第309行分支的纯文本persist条件是死代码。但此分支退出时，messages中最后一条是assistant(tool_calls)（第222-233行append的），这条消息需要yield persist。否则此分支的消息不会被逐条推送写入DB，只能依赖 `persist_agent_reply` 的兜底遍历写入，增加重复风险。

```python
# 第309-316行
if not next_prompt or not next_prompt.strip():
    # 确保最后一轮的 decay 和保存执行
    if on_turn_end is not None:
        on_turn_end(messages, tools_schema, turn)
    # 此分支只在有 tool_calls 的轮次之后进入（纯文本走第295行分支）
    # 需要持久化最后一条 assistant(tool_calls) 消息
    if messages and messages[-1].get("role") == "assistant" and messages[-1].get("tool_calls"):
        yield StreamEvent("persist", json.dumps(messages[-1], ensure_ascii=False))
    if isinstance(should_exit, dict):
        should_exit["messages"] = messages
        return should_exit
    return {"result": "CURRENT_TASK_DONE", "data": None, "messages": messages}
```

#### 1e. working_memory 虚拟消息不 yield persist

working_memory 消息**不需要**逐条持久化。原因：

1. WM 消息是虚拟的内部状态（tool_call_id 以 `wm_` 开头），不推送给前端
2. 在修改2b的 `_persist_one_msg` 中，WM 消息会被过滤掉（不写入 DB）
3. 先 yield 再在消费端过滤，浪费 generator 通道带宽，也增加理解难度
4. WM 消息由 `persist_agent_reply` 的兜底逻辑处理（或被过滤掉）

因此，**删除原方案1e中 yield WM 消息 persist 事件的代码**，不在 WM 消息 append 后 yield 任何 persist 事件。

**影响范围**：
- `StreamEvent` 新增 `"persist"` 类型，不影响现有 `"reply"`、`"tool_marker"`、`"system"` 的消费逻辑
- `agent_runner_loop` 的 generator 多 yield 了一些 `"persist"` 事件，但 `runner.chat()` 中现有代码只处理 `"reply"` 类型，`"persist"` 事件会被静默忽略（直到我们在修改2中添加处理逻辑）
- generator 的 return value 不变，`rv["messages"]` 仍然包含完整的消息列表
- working_memory 虚拟消息不 yield persist 事件（WM 消息不需要逐条持久化，由 `persist_agent_reply` 兜底逻辑处理或被过滤掉）

---

### 修改2：`agent/runner.py` — 消费 `"persist"` 事件，逐条写入 DB + 通知 SSE

#### 2a. 在 `chat()` 方法中处理 `"persist"` 事件（第882-898行区域）

现有代码（第882-898行）：

```python
while True:
    try:
        chunk = next(gen)
        if isinstance(chunk, StreamEvent):
            if chunk.type == "reply":
                full_resp += chunk.content
                if chunk.content:  # SSE 管道：只推送非空 reply
                    yield chunk.content
            # type="system" 和 "tool_marker" 不进入 SSE 和 full_resp
        else:
            # 向后兼容：普通 str
            full_resp += chunk
            if chunk:
                yield chunk
    except StopIteration as e:
        return_value = e.value
        break
```

修改后：

```python
# 在 chat() 方法开头新增：收集逐条持久化的消息列表
persisted_msgs = []  # 已通过 persist 事件持久化的消息（用于兜底去重）

while True:
    try:
        chunk = next(gen)
        if isinstance(chunk, StreamEvent):
            if chunk.type == "reply":
                full_resp += chunk.content
                if chunk.content:  # SSE 管道：只推送非空 reply
                    yield chunk.content
            elif chunk.type == "persist":
                # 新增：逐条持久化消息到 DB + 通知 SSE
                try:
                    msg_dict = json.loads(chunk.content)
                    msg_id = self._persist_one_msg(msg_dict)
                    # 只在 DB 写入成功后才加入已持久化列表（用于兜底去重）
                    if msg_id is not None:
                        persisted_msgs.append(msg_dict)
                except Exception as e:
                    logger.warning(f"[Runner] Failed to persist msg: {e}")
            # type="system" 和 "tool_marker" 不进入 SSE 和 full_resp
        else:
            # 向后兼容：普通 str
            full_resp += chunk
            if chunk:
                yield chunk
    except StopIteration as e:
        return_value = e.value
        break
```

#### 2b. 新增 `_persist_one_msg()` 方法（在 NiuRunner 类中）

```python
def _persist_one_msg(self, msg_dict: dict) -> str | None:
    """逐条持久化消息到 DB + 通知 SSE（同步，从 executor 线程调用）

    Args:
        msg_dict: 完整的消息 dict，包含 role, content, tool_calls, tool_call_id 等

    Returns:
        消息 ID，或 None（写入失败或消息被过滤）
    """
    from niu_api.chat import notify_new_message_sync
    from agent.session import get_message_store

    role = msg_dict.get("role", "")
    content = msg_dict.get("content", "") or ""
    tool_calls = msg_dict.get("tool_calls")
    tool_call_id = msg_dict.get("tool_call_id", "")

    # 跳过 working_memory 虚拟消息（不持久化到 DB，不推送给前端）
    if role == "assistant" and tool_calls:
        if any(tc.get("function", {}).get("name") == "working_memory" for tc in tool_calls):
            return None
    if role == "tool" and tool_call_id.startswith("wm_"):
        return None

    # 同步写入 DB（使用 aiosqlite 的 call_soon_threadsafe 桥接）
    msg_id = self._sync_add_message(role=role, content=content,
                                     tool_calls=tool_calls, tool_call_id=tool_call_id)
    if msg_id is None:
        return None

    # 通知 SSE（仅 assistant 消息推送给前端）
    if role == "assistant" and content.strip():
        notify_new_message_sync(msg_id, "assistant", content, source="electron")

    return msg_id
```

#### 2c. 新增 `_sync_add_message()` 方法（在 NiuRunner 类中）

这是关键桥接方法：从 executor 线程（同步）调用 aiosqlite（异步）的 DB 写入。

```python
def _sync_add_message(self, role: str, content: str,
                       tool_calls: list = None, tool_call_id: str = "") -> str | None:
    """从同步线程写入消息到 DB（桥接 aiosqlite）

    使用 asyncio.run_coroutine_threadsafe 在 FastAPI 事件循环中执行 DB 写入，
    然后阻塞等待结果。这保证了消息按 yield 顺序写入 DB（不会倒序）。

    超时设为30秒：DB写入正常情况下<100ms，30秒足够覆盖极端情况。
    如果30秒仍超时，说明DB严重故障，此时重复写入是可接受的
    （消息丢失比重复更严重）。详见"超时与重复写入"章节。

    Returns:
        消息 ID，或 None（写入失败）
    """
    from niu_api.chat import _main_loop

    loop = _main_loop
    if loop is None or loop.is_closed():
        logger.warning("[Runner] No event loop available for sync DB write")
        return None

    async def _do_add():
        store = await get_message_store()
        return await store.add_message(
            role=role, content=content,
            tool_calls=tool_calls, tool_call_id=tool_call_id
        )

    try:
        future = asyncio.run_coroutine_threadsafe(_do_add(), loop)
        msg_id = future.result(timeout=30.0)  # 阻塞等待，保证顺序
        return msg_id
    except Exception as e:
        logger.warning(f"[Runner] sync_add_message failed: {e}")
        return None
```

需要在文件顶部添加 `import asyncio`（如果尚未导入）。

#### 2d. 暴露 `persisted_msgs` 给调用方

在 `chat()` 方法的 generator 循环结束后（第898行后），将 `persisted_msgs` 存储到实例变量：

```python
# 第900-901行（现有代码）
self.last_return_value = return_value

# 新增
self._persisted_msgs = persisted_msgs  # 已逐条持久化的消息列表
```

**影响范围**：
- `runner.chat()` 的 generator 多处理了 `"persist"` 事件，但不影响现有的 `"reply"` 事件处理
- `_sync_add_message()` 使用 `run_coroutine_threadsafe` + `future.result()` 阻塞等待，保证消息按 yield 顺序写入 DB
- `notify_new_message_sync` 已有现成实现，使用 `call_soon_threadsafe` 注入到 FastAPI 事件循环

---

### 修改3：`niu_api/chat.py` — 修复 `persist_agent_reply` 的重复拼接 + 兜底去重

#### 3a. 修复问题1的根因：修改第178-181行的额外写入条件

```python
# 修改前（第178-181行）
# 纯文本回复不在 rv["messages"] 中，需要从 full_reply 持久化
if full_reply.strip() and full_reply.strip() != last_assistant_content.strip():
    pid = await store.add_message(role="assistant", content=full_reply)
    last_assistant_id = pid

# 修改后：仅在逐条推送未执行时保留额外写入（兜底纯文本回复）
if not persisted_msgs and full_reply.strip() and full_reply.strip() != last_assistant_content.strip():
    pid = await store.add_message(role="assistant", content=full_reply)
    last_assistant_id = pid
```

**三种路径分析**：
- **正常路径**（逐条推送成功）：`persisted_msgs` 非空 → `not persisted_msgs` 为 False → 跳过额外写入 → 不重复
- **兜底路径**（逐条推送未执行）：`persisted_msgs` 为空/None → `not persisted_msgs` 为 True → 保留额外写入 → 纯文本不丢失
- **部分失败路径**：`persisted_msgs` 非空 → 跳过额外写入 → 失败的消息由指纹去重后的 `persist_agent_reply` 兜底写入

#### 3b. 新增 `persisted_msgs` 参数，用于兜底去重

修改 `persist_agent_reply` 函数签名：

```python
# 修改前
async def persist_agent_reply(
    store, rv, history_len: int, full_reply: str, source: str = "electron"
) -> tuple[str | None, str]:

# 修改后
async def persist_agent_reply(
    store, rv, history_len: int, full_reply: str, source: str = "electron",
    persisted_msgs: list[dict] | None = None
) -> tuple[str | None, str]:
```

#### 3c. 在 `persist_agent_reply` 中使用 `persisted_msgs` 去重

在遍历 `rv["messages"]` 写入 DB 之前，先检查哪些消息已被逐条推送写入：

```python
# 在第142行（if rv and isinstance(rv, dict) and rv.get("messages"):）之后插入

# 构建"已持久化消息"指纹集合，用于去重
_persisted_fingerprints = set()
if persisted_msgs:
    for pm in persisted_msgs:
        fp = _msg_fingerprint(pm)
        if fp:
            _persisted_fingerprints.add(fp)
```

在写入每条消息之前检查指纹：

```python
# 在第153行（for msg in rv["messages"][history_len + 1:]:）循环中，
# 每次写入前检查：

for msg in rv["messages"][history_len + 1:]:
    role = msg.get("role", "")
    content = msg.get("content", "")
    tool_calls = msg.get("tool_calls")
    tool_call_id = msg.get("tool_call_id", "")

    if role == "system":
        continue
    if role == "user":
        continue

    # 兜底去重：如果此消息已被逐条推送写入 DB，跳过
    fp = _msg_fingerprint(msg)
    if fp and fp in _persisted_fingerprints:
        continue

    # ... 后续写入逻辑不变 ...
```

#### 3d. 新增 `_msg_fingerprint()` 辅助函数

```python
def _msg_fingerprint(msg: dict) -> str | None:
    """生成消息指纹，用于去重判断

    指纹规则：
    - assistant(tool_calls): role + tool_calls[0].id（唯一标识一次工具调用）
    - assistant(纯文本): role + content[:50]（短文本前缀，避免长文本哈希）
    - tool: role + tool_call_id（唯一标识一次工具结果）
    - 其他: None（不参与去重）
    """
    role = msg.get("role", "")
    if role == "assistant":
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            # assistant(tool_calls)：用第一个 tool_call 的 id 作为指纹
            first_id = tool_calls[0].get("id", "") if tool_calls else ""
            return f"assistant:tc:{first_id}"
        else:
            # 纯文本 assistant：用 content 前50字符
            content = (msg.get("content", "") or "")[:50]
            return f"assistant:text:{content}"
    elif role == "tool":
        tool_call_id = msg.get("tool_call_id", "")
        return f"tool:{tool_call_id}"
    return None
```

#### 3e. SSE 通知调整

现有代码在第184-186行只推送一次 SSE 通知（最后一条 assistant 消息）。逐条推送后，每条 assistant 消息已经通过 `notify_new_message_sync` 推送了。兜底的 `persist_agent_reply` 不再需要推送 SSE，因为：

- 如果逐条推送已执行，前端已收到所有 SSE 通知
- 如果逐条推送未执行（回退路径），`persist_agent_reply` 仍需推送

修改逻辑：

```python
# 修改前（第183-186行）
# 推送最后一条 assistant 消息给 SSE 订阅者
if last_assistant_id:
    message_id = last_assistant_id
    await notify_new_message(message_id, "assistant", full_reply, source=source)

# 修改后
# 推送 SSE 通知（仅在兜底路径：逐条推送未执行时）
if last_assistant_id and not persisted_msgs:
    message_id = last_assistant_id
    await notify_new_message(message_id, "assistant", full_reply, source=source)
elif last_assistant_id:
    # 逐条推送已执行，只需返回 message_id
    message_id = last_assistant_id
```

**影响范围**：
- `persist_agent_reply` 新增 `persisted_msgs` 参数，默认 `None`，向后兼容
- 修改第178-181行的额外写入条件（仅逐条推送未执行时保留），修复问题1（内容重复拼接）
- 兜底去重逻辑仅在 `persisted_msgs` 非空时生效，不影响回退路径

---

### 修改4：`niu_api/compat.py` — 传递 `persisted_msgs` 给 `persist_agent_reply`

#### 4a. `/api/chat/session` 端点（第540-543行区域）

```python
# 修改前
rv = getattr(runner, "last_return_value", None)
from niu_api.chat import persist_agent_reply
message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron")

# 修改后
rv = getattr(runner, "last_return_value", None)
persisted_msgs = getattr(runner, "_persisted_msgs", None)
from niu_api.chat import persist_agent_reply
message_id, full_reply = await persist_agent_reply(
    store, rv, history_len, full_reply, source="electron",
    persisted_msgs=persisted_msgs
)
```

**影响范围**：仅传递新参数，不影响其他逻辑

---

### 修改5：`niu_api/chat.py` — `/chat/sync` 和 `/chat` 端点也传递 `persisted_msgs`

#### 5a. `/chat/sync` 端点（第461-463行区域）

```python
# 修改前
rv = getattr(runner, "last_return_value", None)
history_len = len(history_for_runner) if history_for_runner else 0
message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron")

# 修改后
rv = getattr(runner, "last_return_value", None)
persisted_msgs = getattr(runner, "_persisted_msgs", None)
history_len = len(history_for_runner) if history_for_runner else 0
message_id, full_reply = await persist_agent_reply(
    store, rv, history_len, full_reply, source="electron",
    persisted_msgs=persisted_msgs
)
```

#### 5b. `/chat` 流式端点（第357-359行区域）

```python
# 修改前
rv = getattr(runner, "last_return_value", None)
history_len = 0
message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source="electron")

# 修改后
rv = getattr(runner, "last_return_value", None)
persisted_msgs = getattr(runner, "_persisted_msgs", None)
history_len = 0
message_id, full_reply = await persist_agent_reply(
    store, rv, history_len, full_reply, source="electron",
    persisted_msgs=persisted_msgs
)
```

---

### 修改6：`niu_api/chat_queue.py` — ChatQueue 也传递 `persisted_msgs`

#### 6a. `_process_single` 方法（第316-317行区域）

```python
# 修改前
rv = getattr(self._runner, "last_return_value", None)
message_id, full_reply = await persist_agent_reply(store, rv, history_len, full_reply, source=channel)

# 修改后
rv = getattr(self._runner, "last_return_value", None)
persisted_msgs = getattr(self._runner, "_persisted_msgs", None)
message_id, full_reply = await persist_agent_reply(
    store, rv, history_len, full_reply, source=channel,
    persisted_msgs=persisted_msgs
)
```

---

### 修改7：`ui/main/windows/assistant/chat.html` — 前端 `refreshFromDB` 并发保护优化

#### 7a. 将并发丢弃改为排队等待

现有代码（第1016-1018行）：

```javascript
let _refreshFromDBRunning = false;
async function refreshFromDB() {
  if (_refreshFromDBRunning) return;  // 跳过并发调用
```

问题：当 `refreshFromDB` 正在执行时收到新的 SSE 通知，会直接丢弃。如果两次通知之间间隔很短（逐条推送场景），第二次通知可能被丢弃，导致消息丢失。

修改为：将并发丢弃改为排队等待（最多排1个）：

```javascript
let _refreshFromDBRunning = false;
let _refreshPending = false;  // 是否有排队的刷新请求
async function refreshFromDB() {
  if (_refreshFromDBRunning) {
    _refreshPending = true;  // 排队，不丢弃
    return;
  }
  _refreshFromDBRunning = true;
  try {
    // ... 现有刷新逻辑不变 ...
  } catch (e) {
    console.error('[Chat] 刷新消息失败:', e);
  } finally {
    _refreshFromDBRunning = false;
    // 如果刷新期间有新通知，再刷新一次
    if (_refreshPending) {
      _refreshPending = false;
      // 用 setTimeout 避免递归栈溢出
      setTimeout(() => refreshFromDB(), 0);
    }
  }
}
```

**影响范围**：
- 前端不会丢失任何 SSE 通知触发的刷新
- 最多延迟一次刷新（50ms 级别），用户无感知
- 不影响现有的去重逻辑（`addMessageWithId` 检查 `data-id` 避免重复渲染）

---

### 修改8：`agent/session.py` — 添加 WAL 模式 + busy_timeout

逐条推送场景下，`_sync_add_message` 从 executor 线程高频写入 DB，aiosqlite 可能出现 "database is locked" 错误。需要在 `init_db()` 中添加 WAL 模式和 busy_timeout。

#### 8a. 在 `init_db()` 中添加 PRAGMA 设置

在 `init_db()` 函数的 `CREATE TABLE IF NOT EXISTS` 语句之前插入：

```python
# 在 init_db() 中，CREATE TABLE 之前插入
await db.execute("PRAGMA journal_mode=WAL")
await db.execute("PRAGMA busy_timeout=5000")
```

**说明**：
- `PRAGMA journal_mode=WAL`：启用 Write-Ahead Logging 模式，允许读写并发（读不阻塞写，写不阻塞读），显著提升高频写入场景的并发性能
- `PRAGMA busy_timeout=5000`：当 DB 被锁时，等待最多 5 秒再报错（而不是立即报 "database is locked"），给并发写入足够的等待时间

**影响范围**：
- WAL 模式是 SQLite 的标准特性，不影响数据完整性
- busy_timeout 仅影响锁等待行为，不改变写入逻辑
- 这两个 PRAGMA 是 SQLite 高并发场景的标配，风险极低

---

## 如何避免倒序

倒序是 V1-V3 方案的核心问题。V4 方案通过以下机制保证顺序：

1. **generator 的 yield 是严格有序的**：Python generator 的 `yield` 语句按代码执行顺序依次产出，不可能乱序。

2. **`_sync_add_message` 使用 `future.result()` 阻塞等待**：每次 DB 写入都阻塞等待完成，然后才处理下一个 yield 的事件。这保证了 DB 写入顺序与 yield 顺序一致。

3. **不使用 fire-and-forget**：V1-V3 的倒序根因是 fire-and-forget（异步写入不等待完成，后发起的写入可能先完成）。V4 的 `_sync_add_message` 严格同步等待，不存在此问题。

4. **SSE 通知在 DB 写入之后**：`_persist_one_msg` 先调用 `_sync_add_message`（阻塞等待 DB 写入完成），再调用 `notify_new_message_sync`。前端收到 SSE 通知时，消息一定已经在 DB 中。

5. **`created_at` 时间戳天然有序**：`_sync_add_message` 串行执行，每条消息的 `created_at` 严格递增。即使 DB 写入有微小延迟，时间戳也保证查询结果按正确顺序返回。

---

## 超时与重复写入

`_sync_add_message` 使用 `future.result(timeout=30.0)` 阻塞等待 DB 写入完成。超时策略和重复写入的权衡：

1. **超时时间选择**：DB 写入正常情况下 <100ms，30 秒足够覆盖极端情况（如 WAL checkpoint、大量并发写入等）。如果 30 秒仍超时，说明 DB 严重故障（如磁盘满、锁死等），此时应用层已无法保证数据一致性。

2. **超时后的重复写入风险**：当 `future.result(timeout=30.0)` 超时时，DB 操作可能仍在执行（只是我们没等到结果）。如果 DB 操作最终成功，`persisted_msgs` 不包含这条消息（因为超时返回 None），`persist_agent_reply` 的指纹去重也不包含这条消息的指纹 → `persist_agent_reply` 会重新写入 → 消息重复。

3. **权衡决策**：在 DB 严重故障的极端场景下，重复写入是可接受的。原因：
   - DB 故障时，消息丢失比重复更严重
   - 重复消息可以通过前端去重（`addMessageWithId` 的 `data-id` 检查）和人工清理处理
   - 消息丢失则完全不可恢复
   - 30 秒超时是极端情况的最后防线，正常情况下不会触发

4. **降级路径**：如果超时频繁发生，说明 DB 层面存在问题，应优先排查 DB 性能（检查 WAL 文件大小、busy_timeout 设置、磁盘 IO 等），而非在应用层增加更复杂的去重逻辑。

---

## `persist_agent_reply` 兜底逻辑处理

`persist_agent_reply` 仍然保留，作为兜底路径：

1. **正常路径**：`persisted_msgs` 非空 → 逐条推送已执行 → `persist_agent_reply` 通过指纹去重，跳过已写入的消息，只处理遗漏的消息（理论上不应该有遗漏）

2. **回退路径**：`persisted_msgs` 为 None（逐条推送未执行，如 `_main_loop` 不可用）→ `persist_agent_reply` 按原有逻辑写入所有消息 + SSE 通知

3. **修改第178-181行的额外写入条件**：仅当 `persisted_msgs` 为空时保留额外写入，作为纯文本回复的兜底。逐条推送已执行时（`persisted_msgs` 非空），跳过额外写入，避免重复。

4. **SSE 通知去重**：如果逐条推送已执行（`persisted_msgs` 非空），`persist_agent_reply` 不再推送 SSE 通知，避免重复。

---

## 前端 `refreshFromDB` 并发保护配合

逐条推送场景下，SSE 通知频率从"1次/chat"变为"N次/chat"（N = 消息条数）。前端需要处理高频通知：

1. **排队等待代替并发丢弃**：修改7将 `_refreshFromDBRunning` 的并发保护从"丢弃"改为"排队"，确保不会丢失通知。

2. **`addMessageWithId` 的去重逻辑**：现有的 `data-id` 检查保证即使 `refreshFromDB` 被多次触发，同一条消息也不会重复渲染。

3. **typing 动画关闭时机**：现有逻辑在 `refreshFromDB` 中检测到 assistant 消息时关闭 typing。逐条推送后，第一条 assistant 消息到达时就会关闭 typing，后续消息通过 `addMessageWithId` 追加渲染。

4. **滚动到底部**：每次 `refreshFromDB` 有新消息时都会 `messages.scrollTop = messages.scrollHeight`，保证新消息可见。

---

## 修改文件汇总

| 文件 | 修改点 | 风险等级 |
|------|--------|----------|
| `agent/generic/agent_loop.py` | 新增 `"persist"` 事件类型 + 4处 yield（1a-1d） | **高**（核心循环） |
| `agent/runner.py` | 消费 `"persist"` 事件 + 2个新方法 | **中** |
| `niu_api/chat.py` | 修复重复拼接 + 兜底去重 + SSE 去重 | **中** |
| `niu_api/compat.py` | 传递 `persisted_msgs` 参数 | **低** |
| `niu_api/chat_queue.py` | 传递 `persisted_msgs` 参数 | **低** |
| `ui/main/windows/assistant/chat.html` | 并发保护优化 | **低** |
| `agent/session.py` | WAL 模式 + busy_timeout | **低** |

---

## 验证方法

### 测试1：内容重复拼接修复

**步骤**：
1. 启动应用，发送一个需要多轮工具调用的请求（如"帮我搜索知识图谱中关于XXX的信息"）
2. 等待回复完成
3. 刷新页面，从 DB 加载历史消息
4. 检查：每条 assistant 消息的 content 不应包含前面消息的内容

**验证 SQL**：
```sql
SELECT id, role, length(content), content FROM messages
WHERE role = 'assistant'
ORDER BY created_at DESC
LIMIT 10;
```
检查最后几条 assistant 消息的 content 是否有重复拼接。

### 测试2：逐条推送

**步骤**：
1. 打开浏览器开发者工具的 Network 面板，筛选 EventStream
2. 发送一个需要多轮工具调用的请求
3. 观察 SSE 事件流：应该在 chat 过程中收到多个 `new_message` 事件，而不是只在最后收到一个

**验证**：
- SSE 事件流中应有 N 个 `new_message` 事件（N = assistant 消息条数）
- 每个事件的 `content` 应与 DB 中对应消息的 content 一致
- 前端页面应逐条显示消息，而不是最后一次性显示

### 测试3：消息顺序

**步骤**：
1. 发送一个需要3轮以上工具调用的请求
2. 等待回复完成
3. 查询 DB 中消息的 `created_at` 顺序

**验证 SQL**：
```sql
SELECT id, role, created_at FROM messages
ORDER BY created_at ASC;
```
检查消息顺序应为：user → assistant(tool_calls) → tool → assistant(tool_calls) → tool → ... → assistant(纯文本)

### 测试4：兜底路径

**步骤**：
1. 模拟 `_main_loop` 不可用的场景（如在 runner 初始化前发送请求）
2. 检查 `persist_agent_reply` 的兜底逻辑是否正常工作
3. 消息应全部写入 DB，SSE 通知应在最后推送一次

### 测试5：前端并发保护

**步骤**：
1. 发送一个需要多轮工具调用的请求
2. 在 chat 过程中，观察控制台日志
3. 检查 `_refreshPending` 是否被正确设置和消费
4. 确认没有消息丢失（DB 中的消息数 = 前端渲染的消息数）

### 测试6：纯文本回复（无工具调用）

**步骤**：
1. 发送一个简单的问候（如"你好"），不需要工具调用
2. 检查 assistant 消息是否正确写入 DB
3. 检查 SSE 通知是否正常推送
4. 检查前端是否正确显示

---

## 回退计划

如果 V4 方案出现问题，可以安全回退：

1. **`agent_loop.py`**：删除所有 `yield StreamEvent("persist", ...)` 行，恢复 `_VALID_STREAM_TYPES` 为 `("reply", "tool_marker", "system")`
2. **`runner.py`**：删除 `"persist"` 事件处理逻辑，删除 `_persist_one_msg` 和 `_sync_add_message` 方法
3. **`chat.py`**：恢复第178-181行的额外写入条件（移除 `not persisted_msgs` 条件），删除 `persisted_msgs` 参数和去重逻辑
4. **`compat.py` / `chat_queue.py`**：删除 `persisted_msgs` 传递
5. **`chat.html`**：恢复并发丢弃逻辑
6. **`session.py`**：删除 WAL 模式和 busy_timeout 的 PRAGMA 设置

回退后系统恢复到基线状态，不影响任何现有功能。

---

## 与 V1-V3 方案的对比

| 维度 | V1-V3 | V4 |
|------|-------|-----|
| 推送方式 | fire-and-forget（异步不等待） | 同步阻塞等待（`future.result()`） |
| 顺序保证 | 无（异步写入可能乱序完成） | 强保证（yield 顺序 = DB 写入顺序） |
| 倒序风险 | 高（已证实） | 无（同步串行写入） |
| 修改范围 | 大（新增 ChatQueue 集成、channel 适配） | 小（复用现有 generator 通道） |
| 兜底逻辑 | 无（fire-and-forget 失败时消息丢失） | 有（`persist_agent_reply` 兜底） |
| SSE 通知时机 | 不确定（异步完成时） | 确定（DB 写入完成后） |
| 前端改动 | 大 | 小（并发保护优化） |

---

## 时序图

### 修改前（基线）

```
用户发送消息
  → /api/chat/session
    → sync_chat() 在 executor 线程中执行
      → runner.chat() 消费 generator
        → yield StreamEvent("reply", ...) × N
        → return rv (含完整 messages)
      → full_reply = 所有 reply 的拼接
    → persist_agent_reply(store, rv, history_len, full_reply)
      → 遍历 rv["messages"] 写入 DB（一次性）
      → 额外写入 full_reply（重复拼接！）
      → notify_new_message()（一次 SSE 通知）
  → 前端收到 SSE → refreshFromDB() → 一次性渲染所有消息
```

### 修改后（V4）

```
用户发送消息
  → /api/chat/session
    → sync_chat() 在 executor 线程中执行
      → runner.chat() 消费 generator
        → yield StreamEvent("reply", "第1轮回复")
        → yield StreamEvent("persist", assistant_msg_1)  ← 新增
          → _persist_one_msg() → _sync_add_message() → DB写入 → SSE通知
        → yield StreamEvent("persist", tool_msg_1)      ← 新增
          → _persist_one_msg() → _sync_add_message() → DB写入
        → yield StreamEvent("reply", "第2轮回复")
        → yield StreamEvent("persist", assistant_msg_2)  ← 新增
          → _persist_one_msg() → _sync_add_message() → DB写入 → SSE通知
        → ... 更多轮次 ...
        → yield StreamEvent("persist", final_assistant_msg)  ← 新增
          → _persist_one_msg() → _sync_add_message() → DB写入 → SSE通知
        → return rv (含完整 messages)
    → persist_agent_reply(store, rv, history_len, full_reply, persisted_msgs)
      → 指纹去重：跳过已写入的消息
      → 不再额外写入 full_reply（当逐条推送已执行时）
      → 不再推送 SSE（逐条推送已完成）
  → 前端逐次收到 SSE → refreshFromDB() → 逐条渲染消息
```
