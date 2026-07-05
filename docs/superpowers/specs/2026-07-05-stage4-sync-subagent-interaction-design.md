# 阶段四：同步子 Agent 交互通道设计（v2 — 调研后重写）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让同步调用的子 Agent 也能用 `@niu` / `@end` 前缀表达意图，与主 Agent 对话时底层走 MCP 工具返回值通道（不退出主 Agent 工具循环），消息格式与异步路径完全一致。

**Architecture:** 同步子 Agent 输出 `@niu 问题` 时，agent_runner_loop 拦截层识别后挂起 session（messages 末尾保留 assistant，state="waiting_for_answer"），把 messages/handler/client/tools_schema/system_message 全套状态存到 SubagentRegistry；问题包装成 `[子名] 问题` 作为 yield reply + return MAX_TURNS_EXCEEDED（复用现有 EXIT 机制），call_subagent 拿到 result_text 返给主 Agent。主 Agent LLM 看到工具结果后按 niu.md 提示词生成 `@子名 回答`，重调 `chat-with-xxx(answer, unique_name)`；call_subagent 检测到 answer 参数走第三分支，从 registry 拿回 suspended session，append user 消息（主 Agent 回答）后作为 `resumed_messages` 重新调 `_run_agent_loop`，agent_runner_loop 跳过 history+user 构造直接跑。程序触发子 Agent（无主 Agent 在线）由 `call_subagent_with_auto_answer` helper 自动回复固定文案。所有子 Agent（同步+异步）强制注入 @niu/@end 守则段。

**Tech Stack:** Python（agent_loop.py / subagent.py / subagent_registry.py / handler.py / runner.py / compat.py），纯内存 SubagentRegistry，OpenAI tool schema（chat-with-xxx 加可选参数）。

---

## 1. 背景与动机

### 1.1 阶段三遗留问题

阶段三完成了异步子 Agent 的 @前缀交互通道，但同步子 Agent 调用时 `memory_context=None`，被拦截层 NO_INTERCEPTION 跳过——同步子 Agent 仍走旧逻辑：无 tool_calls 即当作会话结束，content 作为工具返回值送回主 Agent，子 Agent 线程退出。

旧逻辑在子 Agent "遇到困难但未明确退出"时出问题：子 Agent 会以"提问文本"作为 content 直接返回，主 Agent 看到的是问题文本但已无子 Agent session 可回复，造成工作未完成。

### 1.2 阶段三实施过程中暴露的次要问题

- 阶段三回退了"结构注入 ask_main_agent 守则"代码（commit `0ee5660f`），导致子 Agent 第一次输出不知道用 @niu 前缀，会先触发 FORMAT_ERROR 后第二轮才学会
- 守则注入应同时覆盖同步和异步子 Agent，不能只针对异步

### 1.3 阶段四目标

- 同步子 Agent 也走 @前缀拦截层，与异步路径行为一致
- 消息格式统一：同步和异步的 @niu 问题都包装成 `[子名] 问题`，主 Agent 回复都包装成 `@子名 回答`
- 传输通道分离：异步走 db_monitor 链路 A→B（主 Agent 闲置触发新一轮），同步走"工具返回值 → 主 Agent 工具循环内回答 → 工具再次调用回送"
- 程序触发子 Agent 由 `call_subagent_with_auto_answer` helper 自动回复固定文案
- 所有子 Agent 强制注入 @niu/@end 守则段

---

## 2. 核心设计原则

1. **消息格式统一**——同步和异步的 @niu 问题都包装成 `[子名] 问题`，主 Agent 回复都包装成 `@子名 回答`。主 Agent LLM 不感知对方是同步还是异步。
2. **传输通道分离**——异步走 db_monitor 链路 A→B；同步走 MCP 工具返回值通道，主 Agent 在同一轮工具调用内回答。
3. **session 状态全量存 SubagentRegistry**——同步子 Agent 遇 @niu 时把 messages / handler / client / tools_schema / system_message 全套状态存到 registry（新增字段）。第二次 call_subagent 重新调 `_run_agent_loop`，**不是 continue 生成器**（生成器已 StopIteration 销毁，不可能 continue）。
4. **所有子 Agent 注入 @niu/@end 守则**——同步和异步统一注入。
5. **程序触发点包 while 循环**——auto_tidy / force 压缩 / 手动 tidy API 等场景，收到 @niu 自动回复固定文案。

---

## 3. 数据流

### 3.1 用户触发同步子 Agent 的完整流程

```
1. 主 Agent LLM 调 chat-with-xxx(task="做 X")
   ↓
2. handler.dispatch → _call_subagent_gen → call_subagent(task="做 X", agent_name="xxx")
   ↓
3. call_subagent 检测到无 answer 参数 + 无 unique_name → 走 L696 同步新任务分支：
   - 生成 unique_name = "xxx-ab12"
   - 创建 supplement_queue + register 到 SubagentRegistry（state="running"）
   - handler._subagent_unique_name = unique_name
   - handler._is_subagent = True
   - handler._is_sync_subagent = True  ← 新增
   ↓
4. _run_agent_loop(...) → agent_runner_loop(memory_context=None, ...)
   ↓
5. 子 Agent LLM 输出 "@niu 我该选哪个？"
   ↓
6. _intercept_at_prefix_content 拦截（拦截条件改动后见 §4.1）：
   - 检测到 @niu + is_sync_subagent=True → 走同步分支
   - 调 _ask_main_agent_impl_sync(question, unique_name, handler, messages)
     • 不阻塞，立即返回
     • 包装问题为 "[xxx-ab12] 我该选哪个？"
     • messages 末尾 append assistant content（"@niu 我该选哪个？"）  ← 关键：保留对话历史
     • 不 append user（user 由第二次 call_subagent 注入）
   - 返回 INTERCEPTED_SYNC
   ↓
7. agent_runner_loop 收到 INTERCEPTED_SYNC → yield StreamEvent("reply", "[xxx-ab12] 我该选哪个？") + break
   → break 后落到函数末尾 return {"result": "MAX_TURNS_EXCEEDED", "messages": messages, ...}
   （复用现有 EXIT 分支机制，但 messages 末尾是 assistant 而非空）
   ↓
8. _run_agent_loop 收到 StopIteration.value = {"result": "MAX_TURNS_EXCEEDED", "messages": messages}
   返回 (result_text="[xxx-ab12] 我该选哪个？", return_value=dict)
   ↓
9. call_subagent L727-751 后处理：
   - return_value["result"] == "MAX_TURNS_EXCEEDED" 在 control_flow_results 集合中 → _extract_result_from_return_value 返回 None
   - call_subagent 返回 result_text = "[xxx-ab12] 我该选哪个？"
   - **关键改动**：在返回前，把挂起状态存到 registry：
     • instance.state = "waiting_for_answer"
     • instance.suspended_messages = return_value["messages"]
     • instance.suspended_handler = handler
     • instance.suspended_client = client
     • instance.suspended_tools_schema = tools_schema
     • instance.suspended_system_message = system_message
   - **关键改动**：finally 块条件化 unregister——state="waiting_for_answer" 时跳过 unregister
   ↓
10. call_subagent 返回 "[xxx-ab12] 我该选哪个？" → _call_subagent_gen 包成 StepOutcome({"status":"success","result":"[xxx-ab12] 我该选哪个？""})
    → 作为 tool 消息 append 到主 Agent messages
   ↓
11. 主 Agent LLM 看到 chat-with-xxx 工具结果 = "[xxx-ab12] 我该选哪个？"
    → 按 niu.md 提示词生成回复 "@xxx-ab12 选 A"
    → 调 chat-with-xxx(task="", answer="@xxx-ab12 选 A", unique_name="xxx-ab12")
    ↓
12. handler.dispatch → _call_subagent_gen → call_subagent(answer="@xxx-ab12 选 A", answer_unique_name="xxx-ab12")
    检测到 answer is not None → 走第三分支（L671 之前，见 §5.2）：
    - 从 SubagentRegistry.get("xxx-ab12") 拿回 suspended session
    - 若 instance 不存在或 state != "waiting_for_answer" → 返回错误文本，不抛异常
    - 剥除 answer 的 "@xxx-ab12 " 前缀 → "选 A"（若前缀不匹配，原样使用，记 warning）
    - suspended_messages.append({"role": "user", "content": "[主 Agent 回答] 选 A"})
    - 把 suspended_messages 作为 resumed_messages 传给 _run_agent_loop
   ↓
13. _run_agent_loop(resumed_messages=suspended_messages, ...) → agent_runner_loop(resumed_messages=...)
    检测到 resumed_messages is not None → 跳过 L416-477 的 messages 构造（system_message + history + user_input），直接用 resumed_messages
    → messages 末尾是 user，LLM 正常处理
    → 继续跑工具循环
   ↓
14. 子 Agent 继续跑 → 输出 "@end 任务完成" 或再次 "@niu"
   ↓
15. @end 路径：拦截层返回 EXIT → agent_runner_loop yield reply "任务完成" + break + return MAX_TURNS_EXCEEDED
    → _run_agent_loop 返回 (result_text="任务完成", return_value={"result":"MAX_TURNS_EXCEEDED","messages":messages})
    → call_subagent 后处理：extract 返回 None → 返回 result_text="任务完成"
    → **关键**：finally 块 state != "waiting_for_answer"（@end 已正常退出）→ 调 unregister 清理 session
   ↓
16. call_subagent 返回 "任务完成" → 主 Agent 工具循环结束
```

### 3.2 与异步路径的差异对比

| 维度 | 异步路径 | 同步路径 |
|------|---------|---------|
| memory_context | 非 None | None |
| handler._is_sync_subagent | False | True |
| @niu 问题送出通道 | push MainAgentRequestQueue → db_monitor 链路 A 检测主 Agent 闲置 → 触发新一轮 LLM | yield reply → 作为 tool 消息送主 Agent → 主 Agent 在当前工具循环内回答 |
| 主 Agent 回复送回通道 | db_monitor 链路 B 路由到 SubagentSupplementQueue | 主 Agent 重调 chat-with-xxx(answer, unique_name) → call_subagent 第三分支注入 |
| session 状态存储 | SubagentRegistry（memory_context 字段） | SubagentRegistry（suspended_messages/handler/client/tools_schema/system_message 字段） |
| 消息格式 | `[子名] 问题` / `@子名 回答` | `[子名] 问题` / `@子名 回答`（完全一致） |
| 主 Agent 是否阻塞 | 不阻塞（异步） | 阻塞在 dispatch 工具调用上（同步） |
| 主 Agent 工具循环是否退出 | 退出（异步路径主 Agent 跨多轮） | 不退出（同一轮工具调用内） |
| 拦截层 @niu 分支 | 异步分支：调 _ask_main_agent_impl 阻塞等回答 → append assistant + user → INTERCEPTED | 同步分支：调 _ask_main_agent_impl_sync 不阻塞 → append assistant 不 append user → INTERCEPTED_SYNC |
| agent_runner_loop 收到拦截返回值后 | INTERCEPTED → continue（LLM 重跑） | INTERCEPTED_SYNC → yield reply + break + return MAX_TURNS_EXCEEDED（call_subagent 返回） |

### 3.3 程序触发子 Agent 的特殊处理

程序触发子 Agent（auto_tidy / force 压缩 / 手动 tidy API）时，没有主 Agent 在工具循环里等着。子 Agent 输出 `@niu 问题` 时由 helper 函数自动回复固定文案。

新封装 `call_subagent_with_auto_answer(agent_name, task, ...)`：

```python
def call_subagent_with_auto_answer(agent_name, task, ...):
    """程序触发子 Agent 专用：自动回复 @niu，遇到 @end 或正常文本才返回。"""
    AUTO_ANSWER = "无法解答你的问题，请选择 @end 结束并汇报你的工作，或自我抉择选择继续工作"
    
    result = call_subagent(agent_name, task, ...)
    while _is_at_niu_question(result, agent_name):
        unique_name = _extract_unique_name(result)
        result = call_subagent(
            agent_name=agent_name,
            task="",
            answer=AUTO_ANSWER,
            answer_unique_name=unique_name,
            ...
        )
    return result
```

**`_is_at_niu_question` 严格匹配**——用正则 `^\[<agent_name>-[0-9a-f]{4}\] ` 精确匹配 unique_name 格式（agent_name + 4 位 hex），避免误判子 Agent 正常结果中的 `[已完成]` / `[注]` / JSON 数组等文本。

**不防御死循环**——工具循环本身有"3 次同工具同参数提醒"机制，自动回复次数不加上限，不强制终止。

**程序触发点清单（待实施时派 Agent 全面排查）**——已知 10 处：
- `niu_api/compat.py:1861/1935/2006/2174/2397/2562/2635/2706/2810`（auto_tidy + 手动 tidy API）
- `agent/runner.py:1223`（_run_context_manager_force）

所有程序触发点替换为 `call_subagent_with_auto_answer`。

---

## 4. 拦截层改造

### 4.1 拦截条件改动

`agent/generic/agent_loop.py:69` 当前条件：

```python
if memory_context is None or tool_calls:
    return NO_INTERCEPTION
```

改为：

```python
is_sync_subagent = getattr(handler, "_is_sync_subagent", False)
if (memory_context is None and not is_sync_subagent) or tool_calls:
    return NO_INTERCEPTION
```

主 Agent 路径（`memory_context=None` + `_is_sync_subagent=False`）仍返回 NO_INTERCEPTION。异步路径（`memory_context is not None`）行为不变。同步子 Agent（`memory_context=None` + `_is_sync_subagent=True`）进入拦截层。

### 4.2 @niu 路径分同步/异步

拦截层检测到 `@niu` 时，根据是否同步走不同函数：

```python
if stripped.startswith("@niu"):
    question = stripped[4:].lstrip()
    if not question: return FORMAT_ERROR
    unique_name = getattr(handler, "_subagent_unique_name", "")
    if not unique_name: return FORMAT_ERROR
    
    is_sync_subagent = getattr(handler, "_is_sync_subagent", False)
    if is_sync_subagent:
        # 同步路径：不阻塞，包装问题返回给主 Agent
        from agent.subagent import _ask_main_agent_impl_sync
        wrapped = _ask_main_agent_impl_sync(
            question=question,
            unique_name=unique_name,
            handler=handler,
            messages=messages,
            content=content,
        )
        # _ask_main_agent_impl_sync 内部：
        #   1. messages.append({"role": "assistant", "content": content})
        #   2. 返回 "[unique_name] question" 包装文本
        # 拦截层返回 INTERCEPTED_SYNC
        return INTERCEPTED
    else:
        # 异步路径：阻塞等主 Agent 回答（现有逻辑）
        from agent.subagent import _ask_main_agent_impl
        answer = _ask_main_agent_impl(question=question, unique_name=unique_name)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"[主 Agent 回答] {answer}"})
        return INTERCEPTED
```

**关键设计点**：
- 同步路径返回值用 `INTERCEPTED_SYNC`（新常量），区别于异步的 `INTERCEPTED`
- 同步路径在 `_ask_main_agent_impl_sync` 内部 append assistant content（保留对话历史），**不 append user**（user 由第二次 call_subagent 注入）
- 同步路径返回的 wrapped 文本（`[unique_name] question`）由调用方（agent_runner_loop）yield 出去

### 4.3 agent_runner_loop 处理 INTERCEPTED_SYNC

`agent/generic/agent_loop.py:568-593` 当前分支：

```python
if interception == INTERCEPTED:
    continue  # 异步路径：LLM 重跑
if interception == EXIT:
    # @end 允许退出
    ...yield reply + break
if interception == FORMAT_ERROR:
    _harness_fail_count = 0
    continue
# NO_INTERCEPTION：继续走原有逻辑
```

新增 INTERCEPTED_SYNC 分支（在 INTERCEPTED 之后、EXIT 之前）：

```python
if interception == INTERCEPTED_SYNC:
    # 同步路径：yield wrapped_text + break + return MAX_TURNS_EXCEEDED
    # wrapped_text 已在 _ask_main_agent_impl_sync 内构造，挂在某处供 agent_runner_loop 取
    wrapped = getattr(handler, "_pending_sync_yield_text", "")
    yield StreamEvent("reply", wrapped)
    break
```

**实现细节**：`_ask_main_agent_impl_sync` 把 wrapped 文本挂到 `handler._pending_sync_yield_text`，agent_runner_loop INTERCEPTED_SYNC 分支取出来 yield + break，break 后落到函数末尾 `return {"result": "MAX_TURNS_EXCEEDED", "messages": messages, ...}`。

### 4.4 @end 路径

同步和异步的 @end 路径行为一致：剥前缀 → yield reply → break → return MAX_TURNS_EXCEEDED。当前 `agent_loop.py:578-589` 的 EXIT 分支已支持，**无需改动**。

### 4.5 FORMAT_ERROR 路径

同步和异步的格式错误处理一致：追加错误提示 + continue。当前 `agent_loop.py:590-592` 已支持，**无需改动**。

---

## 5. call_subagent 双路入口 + 第三分支

### 5.1 函数签名扩展

`agent/subagent.py:573` 的 call_subagent 加两个可选参数：

```python
def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
    history: Optional[list] = None,
    context_fifo_threshold: int = -1,
    no_tools: bool = False,
    supplement_queue: Optional[Any] = None,
    memory_context: Optional[Any] = None,
    unique_name: Optional[str] = None,
    answer: Optional[str] = None,              # 新增：回复路径用
    answer_unique_name: Optional[str] = None,  # 新增：回复路径用，标识要拿回哪个 session
) -> str:
```

### 5.2 第三分支（回复路径）

`subagent.py:671` 现有结构：

```python
if unique_name is not None:
    # 异步新任务分支（L671-695）
else:
    # 同步新任务分支（L696-723）
```

改为三分支（**判断顺序：answer 最先**）：

```python
if answer is not None and answer_unique_name is not None:
    # 第三分支：回复路径
    instance = SubagentRegistry.get(answer_unique_name)
    if instance is None or getattr(instance, "state", None) != "waiting_for_answer":
        return f"[错误] 找不到挂起的子 Agent session（unique_name={answer_unique_name}），可能已被终止"
    
    # 剥除 "@子名 " 前缀（容错：找不到前缀原样使用，记 warning）
    reply_text = _strip_at_prefix(answer, answer_unique_name)
    
    # 注入 user 消息到 suspended_messages
    suspended_messages = instance.suspended_messages
    suspended_messages.append({"role": "user", "content": f"[主 Agent 回答] {reply_text}"})
    
    # 用 suspended 的全套状态重新调 _run_agent_loop
    try:
        result_text, return_value = _run_agent_loop(
            client=instance.suspended_client,
            system_prompt="",
            system_message=instance.suspended_system_message,
            user_input="",  # 回复路径不追加 user
            initial_user_content=None,  # 跳过 L474-477 user append
            handler=instance.suspended_handler,
            tools_schema=instance.suspended_tools_schema,
            memory_context=None,
            resumed_messages=suspended_messages,  # 新参数，跳过 history+user 构造
            ...
        )
    finally:
        SubagentRegistry.unregister(answer_unique_name)  # 回复路径跑完即注销
    
    # 后处理（同 L727-751）
    ...

elif unique_name is not None:
    # 异步新任务分支（不变）
    ...
else:
    # 同步新任务分支（改动：handler._is_sync_subagent = True）
    ...
```

### 5.3 同步新任务分支改动

`subagent.py:696-723` 的同步新任务分支，在 L643 创建 handler 之后加：

```python
handler._is_sync_subagent = True  # 同步路径标记
```

异步新任务分支（L671-695）和异步 _dispatch_async_subagent 路径都要显式设：

```python
handler._is_sync_subagent = False  # 异步路径标记
```

### 5.4 finally unregister 条件化

`subagent.py:723` 的 `finally: SubagentRegistry.unregister(unique_name)` 改为：

```python
finally:
    instance = SubagentRegistry.get(unique_name)
    state = getattr(instance, "state", None) if instance else None
    if state == "waiting_for_answer":
        pass  # 挂起状态，跳过 unregister，等第二次 call_subagent 回复路径注销
    else:
        SubagentRegistry.unregister(unique_name)
```

### 5.5 call_subagent 第一次因 @niu 返回时存挂起状态

`subagent.py:727-751` 后处理之前，加：

```python
# 同步 @niu 路径：return_value["result"] == "MAX_TURNS_EXCEEDED" 且 messages 末尾是 assistant
# 把挂起状态存到 registry
if return_value and isinstance(return_value, dict):
    result_flag = return_value.get("result", "")
    if result_flag == "MAX_TURNS_EXCEEDED" and getattr(handler, "_is_sync_subagent", False):
        # 检查 messages 末尾是否是 @niu 触发的 assistant（不是 @end）
        msgs = return_value.get("messages", [])
        if msgs and msgs[-1].get("role") == "assistant":
            content = msgs[-1].get("content", "")
            if content.lstrip().startswith("@niu"):
                instance = SubagentRegistry.get(unique_name)
                if instance:
                    instance.state = "waiting_for_answer"
                    instance.suspended_messages = msgs
                    instance.suspended_handler = handler
                    instance.suspended_client = client
                    instance.suspended_tools_schema = tools_schema
                    instance.suspended_system_message = system_message
                # finally 块会因 state="waiting_for_answer" 跳过 unregister
```

### 5.6 chat-with-xxx schema 改动

`agent/runner.py:312-393` 的 chat-with-xxx schema 加可选参数：

```python
{
    "name": "chat-with-xxx",
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务描述"},
            "answer": {"type": "string", "description": "回复子 Agent 的 @niu 问题（含 @子名 前缀）"},
            "unique_name": {"type": "string", "description": "子 Agent 唯一名（回复时必填）"},
            "async_mode": {"type": "boolean", ...}  # 已有，allowAsync 时才有
        },
        "required": ["task"]  # task 仍必填，回复路径可传空字符串
    }
}
```

### 5.7 _call_subagent_gen 透传

`agent/handler.py:943-944` 的参数解析扩展：

```python
task = args.get("task", "")
async_mode = args.get("async_mode", False)
answer = args.get("answer")
unique_name_arg = args.get("unique_name")  # 避免与 call_subagent 的 unique_name 参数混淆
```

L998 调 call_subagent 时透传：

```python
result = call_subagent(
    agent_name=agent_name,
    task=task,
    llm_config=llm_config,
    mcp_client=mcp_client,
    answer=answer,
    answer_unique_name=unique_name_arg if answer else None,
)
```

---

## 6. _run_agent_loop 与 agent_runner_loop 改造

### 6.1 _run_agent_loop 新增 resumed_messages 参数

`agent/subagent.py:189-204` 的 `_run_agent_loop` 签名加：

```python
def _run_agent_loop(
    ...,
    resumed_messages: Optional[list] = None,  # 新增：同步回复路径用
) -> tuple:
```

L228 调 agent_runner_loop 时透传：

```python
gen = agent_runner_loop(
    ...,
    resumed_messages=resumed_messages,
)
```

### 6.2 agent_runner_loop 新增 resumed_messages 参数

`agent/generic/agent_loop.py:391-410` 的 `agent_runner_loop` 签名加：

```python
def agent_runner_loop(
    ...,
    resumed_messages: Optional[list] = None,  # 新增
):
```

L416-477 的 messages 构造逻辑加分支：

```python
if resumed_messages is not None:
    # 回复路径：直接用挂起的 messages，跳过 system_message + history + user_input 构造
    messages = resumed_messages
else:
    messages = [system_message]
    # ... 现有 history 处理 + user_input append ...
```

---

## 7. SubagentRegistry 字段扩展

### 7.1 RunningSubagent 新增字段

`agent/subagent_registry.py:21-32` 的 `RunningSubagent` 数据类加 6 个字段：

```python
@dataclass
class RunningSubagent:
    unique_name: str
    agent_type: str
    supplement_queue: Any
    memory_context: Optional[Any] = None
    is_sync: bool = True
    task: Optional[Union[asyncio.Task, ConcurrentFuture]] = None
    started_at: float = field(default_factory=time.time)
    # 新增字段（同步 @niu 挂起状态）
    state: str = "running"  # "running" / "waiting_for_answer"
    suspended_messages: Optional[list] = None
    suspended_handler: Optional[Any] = None
    suspended_client: Optional[Any] = None
    suspended_tools_schema: Optional[list] = None
    suspended_system_message: Optional[dict] = None
```

### 7.2 内存管理

- 挂起状态全内存（与现有 registry 一致），重启即丢失
- 主 Agent 不回复的孤儿 session 由 `/stop` 清理（见 §8.1）
- 单 session 挂起开销：messages 列表 + handler 对象 + LLM client + tools_schema 列表 + system_message dict，约几十 KB~几 MB（取决于 messages 长度）
- 多 session 并发挂起上限不限制（依赖主 Agent 工具循环自然限流）

---

## 8. /stop 与 SubagentRegistry 清理

### 8.1 request_stop_all_subagents 改造

`agent/runner.py:54-71` 的 `request_stop_all_subagents` 加扫描逻辑：

```python
def request_stop_all_subagents():
    for instance in SubagentRegistry.list_running():
        state = getattr(instance, "state", "running")
        if state == "waiting_for_answer":
            # 同步挂起 session：agent_runner_loop 已退出，supplement 推了无人消费
            # 直接 unregister 释放资源
            SubagentRegistry.unregister(instance.unique_name)
        else:
            # 活跃 session（同步 running 或异步）：推 /stop 终止
            pending_ask.cancel_pending_ask(instance.unique_name)
            instance.supplement_queue.push("/stop", is_terminate=True)
```

### 8.2 主 Agent 工具循环退出时清理

主 Agent 工具循环可能因 LLM 不调第二次 chat-with-xxx 而退出（如 LLM 直接回用户）——此时挂起的同步 session 残留。处理：

- 主 Agent 工具循环退出（agent_runner_loop 主路径结束）时，扫描 SubagentRegistry，清理所有 `state="waiting_for_answer"` 且 `is_sync=True` 的 session
- 实现位置：`agent/runner.py` 主 Agent 工具循环结束处加 `cleanup_suspended_sync_subagents()` 调用

---

## 9. 提示词守则注入（所有子 Agent）

### 9.1 守则模板

`agent/subagent.py` 重新引入守则常量（之前 commit `0ee5660f` 删除的，文案改为 @niu/@end 描述）：

```python
_SUBAGENT_ASK_GUIDE_TEMPLATE = """
## 子 Agent 与主 Agent 对话规则

你是子 Agent，工作未完成时遇到必须澄清的问题，必须用 `@niu ` 前缀的 content 询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。

只有以下情况才能直接返回：
1. 任务已完成，用 `@end ` 前缀返回最终结果。
2. 任务确实无法继续（如缺权限、缺资源），用 `@end ` 前缀汇报情况让主 Agent 决策。

其他任何"需要更多信息才能继续"的情况，一律用 `@niu ` 前缀询问。

格式示例：
- 询问：`@niu 我应该选择哪个选项？`
- 结束：`@end 任务已完成，结果：...`
"""

_SUBAGENT_ASK_GUIDE_MARKER = "<!-- NIU_SUBAGENT_GUIDE_v1 -->"
```

守则模板末尾插入 marker 注释，作为去重标记（避免子 Agent 正文含 `## 子 Agent 与主 Agent 对话规则` 标题被误判）。

### 9.2 注入逻辑

`build_subagent_system_segments(agent_name)` 不再加 `allow_async` 参数，统一注入：

```python
# 4. 强制注入 @niu/@end 守则（所有子 Agent）
if _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
    static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
```

### 9.3 模板和文档同步

- `config/agent-template.md` L27 简化——程序统一注入，主 Agent 写 MD 时不必再强调 allowAsync 与守则的关系
- `config/agents/niu.md` L255/L283/L291 同步——同步子 Agent 也会 @niu，主 Agent 处理逻辑一致
- `docs/SYSTEM_MANUAL.md` + `docs/manual-general-subagent.md` 同步更新

---

## 10. 错误处理与边界情况

### 10.1 session 挂起后主 Agent 不调第二次 chat-with-xxx

- 主 Agent LLM 看到 `[子名] 问题` 工具结果后可能不调 chat-with-xxx 回复（LLM 决定不回复、上下文超长被压缩、用户中途 /stop）
- 处理：
  - `/stop`：§8.1 扫描清理
  - 主 Agent 工具循环退出：§8.2 扫描清理
  - 主 Agent 上下文压缩：暂不处理（依赖 §8.2 兜底）

### 10.2 主 Agent 回复时 unique_name 不匹配

- call_subagent 第三分支检测 `instance is None or state != "waiting_for_answer"` → 返回错误文本"找不到挂起的子 Agent session"
- 不抛异常（避免中断主 Agent 工具循环）
- 主 Agent LLM 看到错误文本后会自行决策（重新调 chat-with-xxx 新任务或放弃）

### 10.3 answer 文本不含 @子名 前缀

- `_strip_at_prefix(answer, answer_unique_name)` 找不到前缀时记 warning，原样使用 answer 文本
- 不阻塞流程（LLM 不听话时不应让子 Agent 卡死）

### 10.4 嵌套子 Agent（A 调 B，B @niu）

- 当前阶段不处理嵌套场景——子 Agent 调子 Agent 的路径在 chat-with-* 工具过滤时已被移除（`subagent.py:544` 过滤 `chat-with-`）
- 子 Agent 不能再调子 Agent，无嵌套场景

### 10.5 拦截层条件改动后的兼容性

- 主 Agent 路径（`_is_sync_subagent=False` + `memory_context=None`）→ NO_INTERCEPTION（回归测试覆盖）
- 异步路径（`memory_context is not None`）→ 异步分支不变（回归测试覆盖）
- 同步子 Agent（`memory_context=None` + `_is_sync_subagent=True`）→ 进入拦截层 @niu/@end/FORMAT_ERROR 分支

### 10.6 同步子 Agent 走 @end 时的退出语义

- 拦截层返回 EXIT → agent_runner_loop yield reply + break + return MAX_TURNS_EXCEEDED
- call_subagent 收到 StopIteration → extract 返回 None → 返回 result_text
- finally 块 state != "waiting_for_answer"（@end 不挂起）→ unregister 清理 session
- 主 Agent 看到工具结果 = "@end 后的内容"，作为最终结果注入 LLM

---

## 11. 测试策略

### 11.1 单元测试（mock LLM，验证拦截层和路由逻辑）

1. **拦截层条件改动回归**
   - 同步子 Agent（`_is_sync_subagent=True`, `memory_context=None`）输出 `@niu 问题` → 返回 INTERCEPTED_SYNC，messages 末尾是 assistant
   - 同步子 Agent 输出 `@end 结果` → 返回 EXIT
   - 同步子 Agent 输出无 `@` 前缀 → 返回 FORMAT_ERROR
   - 主 Agent 路径输出任何 content → 返回 NO_INTERCEPTION（回归）
   - 异步子 Agent 所有行为不变（回归）

2. **`_ask_main_agent_impl_sync` 函数**
   - 调用后：messages 末尾 append assistant content
   - 返回文本格式 `[unique_name] question`
   - handler._pending_sync_yield_text 设为 wrapped 文本
   - 不推 MainAgentRequestQueue

3. **call_subagent 三路入口**
   - 无 answer + 无 unique_name → 同步新任务路径
   - 无 answer + 有 unique_name → 异步新任务路径
   - 有 answer + 有 answer_unique_name → 回复路径
   - 回复路径 instance 不存在 → 返回错误文本，不抛异常
   - 回复路径 state != "waiting_for_answer" → 返回错误文本

4. **finally unregister 条件化**
   - state="waiting_for_answer" → 跳过 unregister
   - state="running" → 正常 unregister

5. **chat-with-xxx schema 改动**
   - schema 含可选 answer + unique_name 参数
   - 不带这俩参数 = 新任务调用
   - 带这俩参数 = 回复调用

6. **`call_subagent_with_auto_answer` helper**
   - 第一次 call_subagent 返回正常文本（非 @niu）→ 直接返回
   - 第一次返回 @niu 问题 → 自动回复 → 第二次返回 @end → 返回最终结果
   - 多轮 @niu → 多轮自动回复
   - 子 Agent 正常结果含 `[已完成] 文件 X` → 不误判为 @niu（精确正则匹配）

7. **resumed_messages 参数**
   - agent_runner_loop 收到 resumed_messages → 跳过 system_message + history + user_input 构造
   - 直接用 resumed_messages 跑

8. **提示词注入**
   - 所有子 Agent（同步 + 异步）build_subagent_system_segments 都注入守则段
   - 守则段含 @niu / @end 描述
   - 去重 marker 是 `<!-- NIU_SUBAGENT_GUIDE_v1 -->`
   - 子 Agent 正文已含 marker 时不重复注入

9. **request_stop_all_subagents 改造**
   - state="waiting_for_answer" → 直接 unregister
   - state="running" → 推 /stop 终止

### 11.2 端到端测试（真实 LLM + 真实程序，禁 mock）

1. **同步子 Agent @niu 询问 + 主 Agent 回复 + 子 Agent 继续**
   - 主 Agent 调 chat-with-xxx → 子 Agent @niu 问澄清问题 → 主 Agent 看到 `[子名] 问题` → 回 `@子名 回答` → 子 Agent 收到回答继续工作 → @end 返回结果
   - 验证：主 Agent 工具循环未退出；session 在 registry 里正确注册和清理

2. **同步子 Agent 多轮 @niu**
   - 子 Agent 连续问 3 次 @niu → 主 Agent 回复 3 次 → 子 Agent @end

3. **同步子 Agent @end 直接结束**
   - 子 Agent 不问问题直接 @end → 主 Agent 收到结果 → 工具循环结束

4. **同步子 Agent 格式错误回退**
   - 子 Agent 第一次输出无 @ 前缀 → 触发 FORMAT_ERROR → 第二次输出 @niu

5. **程序触发子 Agent @niu 自动回复**
   - 触发 auto_tidy → 子 Agent @niu → 自动回复固定文案 → 子 Agent @end
   - 验证：固定文案正确送回；不阻塞主 Agent

6. **/stop 终止挂起的同步子 Agent**
   - 同步子 Agent @niu 挂起 → 用户 /stop → registry 清理挂起 session
   - 验证：session 不残留；主 Agent 工具循环正确退出

7. **回归测试**
   - 异步子 Agent 所有行为不变（5 次 @niu + @end + 格式错误 + /stop）
   - 主 Agent 正常对话不被拦截层误伤

---

## 12. 修改的文件清单

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `agent/subagent_registry.py` | RunningSubagent 新增 6 字段（state / suspended_*） | P0 |
| `agent/generic/agent_loop.py` | 拦截条件加 `is_sync_subagent`；@niu 同步分支调 `_ask_main_agent_impl_sync`；新增 INTERCEPTED_SYNC 常量 + agent_runner_loop 处理分支；新增 `resumed_messages` 参数 + 跳过 messages 构造逻辑 | P0 |
| `agent/subagent.py` | 重新引入 `_SUBAGENT_ASK_GUIDE_TEMPLATE` / `_SUBAGENT_ASK_GUIDE_MARKER`；`build_subagent_system_segments` 统一注入守则；新增 `_ask_main_agent_impl_sync`；`call_subagent` 加 `answer` + `answer_unique_name` 参数 + 第三分支；同步新任务分支设 `handler._is_sync_subagent=True`；异步分支设 `handler._is_sync_subagent=False`；`_run_agent_loop` 加 `resumed_messages` 参数；call_subagent 后处理存挂起状态；finally unregister 条件化 | P0 |
| `agent/handler.py` | `_call_subagent_gen` 解析 answer + unique_name 参数 + 透传给 call_subagent | P0 |
| `agent/runner.py` | chat-with-xxx schema 加 answer + unique_name 可选参数；`request_stop_all_subagents` 加挂起 session 扫描清理；主 Agent 工具循环退出时调 `cleanup_suspended_sync_subagents`；程序触发点（`runner.py:1223`）替换为 `call_subagent_with_auto_answer` | P0-P1 |
| `niu_api/compat.py` | 9 个程序触发点替换为 `call_subagent_with_auto_answer` | P1 |
| `config/agent-template.md` | L27 简化守则描述 | P2 |
| `config/agents/niu.md` | L255/L283/L291 同步——同步子 Agent 也会 @niu | P2 |
| `docs/SYSTEM_MANUAL.md` | 同步子 Agent 交互描述更新 | P2 |
| `docs/manual-general-subagent.md` | 通用子 Agent 手册更新 | P2 |
| `tests/test_at_prefix_interception.py` | 加同步路径拦截测试 + INTERCEPTED_SYNC | P0 |
| `tests/test_sync_subagent_interaction.py` | 新建——同步子 Agent 交互单元测试 | P0 |
| `tests/test_call_subagent_with_auto_answer.py` | 新建——helper 单元测试 | P1 |
| `tests/test_subagent_registry.py` | 新增字段测试 + state 转换测试 | P0 |

---

## 13. 实施顺序（粗略，详细 plan 由 writing-plans skill 生成）

1. SubagentRegistry 字段扩展（state + suspended_*）
2. 守则注入恢复（所有子 Agent 统一注入 @niu/@end 守则）—— 立即解决"第一轮 FORMAT_ERROR"问题
3. 拦截层改造（条件加 is_sync_subagent + 新增 INTERCEPTED_SYNC 常量 + 同步 @niu 分支）
4. `_ask_main_agent_impl_sync` 实现
5. agent_runner_loop INTERCEPTED_SYNC 分支 + resumed_messages 参数
6. _run_agent_loop resumed_messages 参数
7. call_subagent 第三分支 + 同步新任务分支设 _is_sync_subagent + finally 条件化 unregister + 后处理存挂起状态
8. chat-with-xxx schema 改动 + _call_subagent_gen 透传
9. `call_subagent_with_auto_answer` helper 实现
10. 派 Agent 全面排查程序触发点 + 替换为 helper
11. request_stop_all_subagents 改造 + 主 Agent 工具循环退出清理
12. 单元测试 + 端到端测试
13. 文档同步

---

## 14. 验收标准

- 所有单元测试通过（含同步路径拦截测试 + helper 测试 + schema 测试 + registry 字段测试）
- 端到端测试 7 个场景全部通过（真实 LLM）
- 异步路径回归无 bug（5 次 @niu + @end + 格式错误 + /stop）
- 主 Agent 正常对话不被拦截层误伤
- 程序触发子 Agent（auto_tidy / force 压缩 / 手动 tidy）@niu 自动回复不阻塞
- 子 Agent 第一次输出就知道用 @niu 前缀（不再触发 FORMAT_ERROR）
- 代码审查通过（spec 合规 + 代码质量两轮，无 BLOCKER）

---

## 15. 相关文档与提交

- 阶段三 spec：`docs/superpowers/specs/2026-07-04-general-subagent-stage3-design.md`
- 阶段三实施完成提交：`10b5bcf4`
- 阶段三回退守则注入提交：`0ee5660f`
- 阶段四 spec v1 提交：`49af81ab`（本文件 v2 替换）
- 阶段四 spec v2 提交：（待生成）

相关记忆：
- [[at-prefix-subagent-intent]] — 阶段三@前缀方案
- [[main-subagent-no-interaction-channel]] — 主子 Agent 交互通道总览
- [[main-subagent-interaction-stage2]] — 阶段二异步路径
- [[stage2-ask-main-agent-stop-deadlock]] — 5 个死锁约束
