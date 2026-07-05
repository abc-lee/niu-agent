# 阶段四：同步子 Agent 交互通道设计（v11 — 基于现有代码事实重写）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让同步调用的子 Agent 也能用 `@niu-agent` / `@end` 前缀表达意图，与主 Agent 对话时底层走 MCP 工具返回值通道（不退出主 Agent 工具循环），消息格式与异步路径完全一致。

**核心思路（基于现有代码事实）：** 阶段三异步路径已经实现了完整的 unique_name 包装机制——子 Agent LLM 只输出 `@niu 问题`（不需要知道 unique_name），Python 拦截层在 `_ask_main_agent_impl`（`subagent.py:810`）自动包装成 `[unique_name] question` 推 MainAgentRequestQueue。同步路径**复用这个机制**——拦截层检测到同步子 Agent 输出 `@niu-agent` 后，调一个新的 `_ask_main_agent_impl_sync` 函数，该函数做和异步 `_ask_main_agent_impl` 几乎一样的事（包装 `[unique_name] question`），但不推 queue、不阻塞——直接把包装文本作为 yield reply 返回给 call_subagent → 主 Agent。主 Agent 看到工具结果 `[xxx-ab12] 问题` 后回复 `@xxx-ab12 回答`，重调 chat-with-xxx 带 answer + unique_name 参数；call_subagent 第三分支从 registry 拿回挂起 session，注入回答后 continue。

**Tech Stack:** Python（agent_loop.py / subagent.py / subagent_registry.py / handler.py / runner.py / compat.py），纯内存 SubagentRegistry，OpenAI tool schema（chat-with-xxx 加可选参数）。

---

## 1. 现有代码事实（重写基础）

### 1.1 阶段三异步路径已实现的机制

| 机制 | 代码位置 | 说明 |
|------|---------|------|
| unique_name 生成 | `subagent_registry.py:40-46` | `secrets.token_hex(2)` 生成 4 位 hex，格式 `<agent_type>-<hex>` |
| 子 Agent 拦截 @niu | `agent_loop.py:75` | `startswith("@niu")` 检测 |
| 调 _ask_main_agent_impl | `agent_loop.py:106-109` | 拦截层调，传 question + unique_name（从 `handler._subagent_unique_name` 读） |
| 包装 [unique_name] question | `subagent.py:810` | `msg_content = f"[{unique_name}] {sanitized_question}"` |
| 推 MainAgentRequestQueue | `subagent.py:815` | 不写 db，纯内存队列 |
| db_monitor 链路 A 推主 Agent | `db_monitor.py:176-213` | 主 Agent 闲置时推 SSE → 前端触发 → compat.py 写 db 最后一条 user 消息 |
| 主 Agent 看到 [子名] 问题 | `niu.md:255` | niu.md 提示词已约束主 Agent 回 `@子名 回答` |
| 主 Agent 回复 @子名 | `at_message_parser.py:12` | 正则 `@([a-z]+(?:-[a-z]+)*-[0-9a-f]{4})\s+(.*?)` 提取 target=unique_name |
| db_monitor 链路 B 路由回子 Agent | `db_monitor.py:76-173` | `route_message` → `PendingAskRegistry.set_answer` → 解除 `future.wait()` 阻塞 |

### 1.2 关键事实

1. **子 Agent LLM 不知道 unique_name**——它只输出 `@niu 问题`，包装由程序做
2. **主 Agent 通过 `[子名] 问题` 包装识别 unique_name**——主 Agent 不需要预先知道 unique_name，从工具结果文本/db 消息里提取
3. **unique_name 包装在 `_ask_main_agent_impl` 内部完成**（`subagent.py:810`），不在拦截层
4. **同步路径当前不拦截 @niu**（`agent_loop.py:69` `memory_context is None` → NO_INTERCEPTION）

### 1.3 阶段三回退的守则注入（commit 0ee5660f）

阶段三曾实现"异步子 Agent 强制注入 ask_main_agent 守则"，后因改用 @前缀方案回退。但回退后子 Agent 第一次输出不知道用 @niu 前缀，会先触发 FORMAT_ERROR 后第二轮才学会。本阶段恢复守则注入，**且对所有子 Agent（同步+异步）统一注入**。

---

## 2. 核心设计原则

1. **复用现有机制**——unique_name 包装、消息格式、主 Agent 回复格式全沿用阶段三异步路径，不重新设计
2. **消息格式统一**——同步和异步的 @niu-agent 问题都是 `[子名] 问题`，主 Agent 回复都是 `@子名 回答`。主 Agent LLM 不感知对方是同步还是异步
3. **传输通道分离**——异步走 db_monitor 链路 A→B；同步走 MCP 工具返回值通道（chat-with-xxx 工具结果 = `[子名] 问题`，主 Agent 重调 chat-with-xxx 带 answer 回复）
4. **session 状态全量存 SubagentRegistry**——同步子 Agent 遇 @niu-agent 时把 messages / handler / client / tools_schema / system_message 全套状态存到 registry（新增字段）。第二次 call_subagent 重新调 `_run_agent_loop`，不是 continue 生成器
5. **所有子 Agent 注入 @niu-agent/@end 守则**——同步和异步统一注入
6. **程序触发点包 while 循环**——auto_tidy / force 压缩 / 手动 tidy API 等场景，收到 @niu-agent 自动回复固定文案

---

## 3. 数据流

### 3.1 用户触发同步子 Agent 的完整流程

```
1. 主 Agent LLM 调 chat-with-xxx(task="做 X")
   ↓
2. handler.dispatch → _call_subagent_gen → call_subagent(task="做 X", agent_name="xxx")
   ↓
3. call_subagent 同步新任务分支（L696-723）：
   - 生成 unique_name = "xxx-ab12"
   - 创建 supplement_queue + register 到 SubagentRegistry
   - handler._subagent_unique_name = unique_name
   - handler._is_subagent = True
   - handler._is_sync_subagent = True  ← 新增
   ↓
4. _run_agent_loop(...) → agent_runner_loop(memory_context=None, ...)
   ↓
5. 子 Agent LLM 输出 "@niu-agent 我该选哪个？"  ← 子 Agent 不需要知道 unique_name
   ↓
6. _intercept_at_prefix_content 拦截（拦截条件改动见 §4.1）：
   - 检测到 @niu-agent + is_sync_subagent=True → 走同步分支
   - 调 _ask_main_agent_impl_sync(question, unique_name, handler, messages, content)
     • 函数内部：messages.append assistant content（保留对话历史）+ 返回 wrapped = "[xxx-ab12] 我该选哪个？"
     • 不推 queue，不阻塞
   - 返回 (INTERCEPTED_SYNC, wrapped)
   - **注意**：messages 里 append 的是原始 content（`@niu-agent 我该选哪个？`），但 agent_runner_loop yield 的 result_text 是 wrapped 文本（`[xxx-ab12] 我该选哪个？`）。两者故意不一致——messages 给子 Agent LLM 看（保留 @niu-agent 前缀上下文），result_text 给主 Agent LLM 看（包装成工具结果格式）。实施时不要把 wrapped 文本 append 到 messages。
   ↓
7. agent_runner_loop 收到 (INTERCEPTED_SYNC, wrapped) → yield StreamEvent("reply", wrapped) + return {"result": "INTERCEPTED_SYNC", "messages": messages, ...}
   ↓
8. _run_agent_loop 收到 StopIteration.value → 返回 (result_text=wrapped, return_value=dict)
   ↓
9. call_subagent 后处理：
   - return_value["result"] == "INTERCEPTED_SYNC" → §5.5 存挂起状态
   - call_subagent 返回 result_text = "[xxx-ab12] 我该选哪个？"
   - finally 块条件化 unregister——state="waiting_for_answer" 时跳过
   ↓
10. _call_subagent_gen 包成 StepOutcome({"status":"success","result":"[xxx-ab12] 我该选哪个？"})
    → 作为 tool 消息 append 到主 Agent messages
   ↓
11. 主 Agent LLM 看到工具结果 JSON = {"status":"success","result":"[xxx-ab12] 我该选哪个？"}
    → 按 niu.md 提示词（§9B）从 result 字段提取问题，生成回复 "@xxx-ab12 选 A"
    → 调 chat-with-xxx(task="", answer="@xxx-ab12 选 A", unique_name="xxx-ab12")
   ↓
12. handler.dispatch → _call_subagent_gen → call_subagent(answer="@xxx-ab12 选 A", answer_unique_name="xxx-ab12")
    检测到 answer is not None → 走第三分支（§5.2）：
    - 从 SubagentRegistry.get("xxx-ab12") 拿回 suspended session
    - 若 instance 不存在或 state != "waiting_for_answer" → 返回错误文本
    - 剥除 answer 的 "@xxx-ab12 " 前缀 → "选 A"
    - suspended_messages.append({"role": "user", "content": "[主 Agent 回答] 选 A"})
    - 把 suspended_messages 作为 resumed_messages 传给 _run_agent_loop
   ↓
13. _run_agent_loop(resumed_messages=suspended_messages, ...) → agent_runner_loop(resumed_messages=...)
    检测到 resumed_messages is not None → 跳过 messages 构造，直接用 resumed_messages
    → messages 末尾是 user，LLM 正常处理
   ↓
14. 子 Agent 继续跑 → 输出 "@end 任务完成" 或再次 "@niu-agent"
   ↓
15. @end 路径：拦截层返回 (EXIT, None) → agent_runner_loop yield reply + return {"result": "EXITED", ...}
    → call_subagent 后处理：§5.5 检测 result_flag != "INTERCEPTED_SYNC" → 不存挂起
    → finally state != "waiting_for_answer" → unregister 清理 session
   ↓
16. call_subagent 返回 "任务完成" → 主 Agent 工具循环结束
```

### 3.2 与异步路径的差异对比

| 维度 | 异步路径 | 同步路径 |
|------|---------|---------|
| memory_context | 非 None | None |
| handler._is_sync_subagent | False | True |
| @niu-agent 问题送出通道 | _ask_main_agent_impl 推 MainAgentRequestQueue → db_monitor 链路 A → 主 Agent 闲置触发新一轮 | _ask_main_agent_impl_sync 直接返回 wrapped → agent_runner_loop yield reply → 作为 tool 消息送主 Agent → 主 Agent 在当前工具循环内回答 |
| 主 Agent 回复送回通道 | db_monitor 链路 B → PendingAskRegistry.set_answer → future.wait() 解除阻塞 | 主 Agent 重调 chat-with-xxx(answer, unique_name) → call_subagent 第三分支注入 user 消息 |
| session 状态存储 | SubagentRegistry（memory_context 字段） | SubagentRegistry（suspended_messages/handler/client/tools_schema/system_message 字段） |
| 消息格式 | `[子名] 问题` / `@子名 回答` | `[子名] 问题` / `@子名 回答`（完全一致） |
| 主 Agent 是否阻塞 | 不阻塞（异步） | 阻塞在 dispatch 工具调用上（同步） |
| 主 Agent 工具循环是否退出 | 退出（异步路径主 Agent 跨多轮） | 不退出（同一轮工具调用内） |
| unique_name 包装位置 | _ask_main_agent_impl（subagent.py:810） | _ask_main_agent_impl_sync（新函数，逻辑相同） |

### 3.3 程序触发子 Agent 的特殊处理

程序触发子 Agent（auto_tidy / force 压缩 / 手动 tidy API）时，没有主 Agent 在工具循环里等着。子 Agent 输出 `@niu-agent 问题` 时由 helper 函数自动回复固定文案。

新封装 `call_subagent_with_auto_answer(agent_name, task, ...)`：

```python
import re

def call_subagent_with_auto_answer(agent_name, task, **kwargs):
    """程序触发子 Agent 专用：自动回复 @niu-agent，遇到 @end 或正常文本才返回。"""
    AUTO_ANSWER = "无法解答你的问题，请选择 @end 结束并汇报你的工作，或自我抉择选择继续工作"
    
    result = call_subagent(agent_name, task, **kwargs)
    while True:
        unique_name = _extract_unique_name(result, agent_name)
        if unique_name is None:
            return result  # 非 @niu-agent 问题，正常返回
        result = call_subagent(
            agent_name=agent_name,
            task="",
            answer=AUTO_ANSWER,
            answer_unique_name=unique_name,
            **kwargs,
        )

def _extract_unique_name(result, agent_name):
    """从 '[unique_name] ...' 提取 unique_name，不匹配返回 None。"""
    pattern = rf"^\[({re.escape(agent_name)}-[0-9a-f]{{4}})\] "
    m = re.match(pattern, result)
    return m.group(1) if m else None
```

**严格正则匹配**——用 `^\[<agent_name>-[0-9a-f]{4}\] ` 精确匹配 unique_name 格式，避免误判子 Agent 正常结果中的 `[已完成]` / `[注]` / JSON 数组等文本。

**不防御死循环**——工具循环本身有"3 次同工具同参数提醒"机制。

**程序触发点清单**（待实施时派 Agent 全面排查）——已知 10 处：
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

### 4.2 拦截层返回值改 tuple

当前拦截层返回 str（INTERCEPTED/EXIT/FORMAT_ERROR/NO_INTERCEPTION）。**改为返回 tuple `(status, payload)`**：

- `(NO_INTERCEPTION, None)`
- `(INTERCEPTED, None)` — 异步 @niu-agent 已处理（messages 已 append assistant + user），agent_runner_loop continue
- `(INTERCEPTED_SYNC, wrapped_text)` — 同步 @niu-agent，agent_runner_loop yield reply + return
- `(EXIT, None)` — @end，agent_runner_loop 剥前缀 yield reply + return
- `(FORMAT_ERROR, None)` — 格式错误，agent_runner_loop continue

**所有现有调用点**（`agent_loop.py:576/578/590`）的 `==` str 比较必须改为 `interception_status ==` 元组首元素。

### 4.3 @niu-agent 路径分同步/异步

```python
if stripped.startswith("@niu-agent"):
    # 剥除 "@niu-agent" 前缀（10 字符）+ 可选空格
    question = stripped[len("@niu-agent"):].lstrip()
    if not question:
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": FORMAT_ERROR_PROMPT})
        return (FORMAT_ERROR, None)
    unique_name = getattr(handler, "_subagent_unique_name", "")
    if not unique_name:
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": FORMAT_ERROR_PROMPT})
        return (FORMAT_ERROR, None)
    
    is_sync_subagent = getattr(handler, "_is_sync_subagent", False)
    if is_sync_subagent:
        # 同步路径：不阻塞，程序包装 [unique_name] question 返回
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
        #   2. 返回 "[unique_name] question"（与异步 _ask_main_agent_impl 包装逻辑一致）
        return (INTERCEPTED_SYNC, wrapped)
    else:
        # 异步路径：阻塞等主 Agent 回答（现有逻辑，subagent.py:810 已包装 [unique_name]）
        from agent.subagent import _ask_main_agent_impl
        answer = _ask_main_agent_impl(question=question, unique_name=unique_name)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"[主 Agent 回答] {answer}"})
        return (INTERCEPTED, None)
```

**关键设计点**：
- 同步路径 `messages` append 的是原始 content（`@niu-agent 问题`），不是 wrapped 文本——保留 @niu-agent 前缀上下文给子 Agent LLM 看
- wrapped 文本（`[unique_name] 问题`）由 agent_runner_loop yield 给 call_subagent，作为 result_text 返给主 Agent LLM
- 两者故意不一致——messages 给子 Agent 看，result_text 给主 Agent 看

#### 4.3.1 `_ask_main_agent_impl_sync` 完整实现

```python
def _ask_main_agent_impl_sync(question: str, unique_name: str, handler, messages: list, content: str) -> str:
    """同步路径：包装 question 为 [unique_name] question，append assistant content 到 messages。

    与异步 _ask_main_agent_impl（subagent.py:810）的包装逻辑一致，但：
    - 不阻塞等主 Agent 回答（同步路径靠工具返回值通道）
    - 不推 MainAgentRequestQueue（同步路径不走 db_monitor）
    - append assistant content 保留对话历史，不 append user（user 由第二次 call_subagent 注入）
    """
    messages.append({"role": "assistant", "content": content})
    # sanitization（与异步路径 subagent.py:807-809 一致，v11 审查 I1）
    sanitized = question[:2000] if question else ""
    if sanitized.lstrip().startswith("@"):
        sanitized = sanitized.lstrip()[1:]
    wrapped = f"[{unique_name}] {sanitized}"
    return wrapped
```

放在 `agent/subagent.py` 的 `_ask_main_agent_impl` 旁边（模块级函数）。

### 4.4 agent_runner_loop 处理拦截层返回值

`agent/generic/agent_loop.py:568-593` 改造（现有 `==` str 比较改元组首元素）：

```python
interception_status, interception_payload = _intercept_at_prefix_content(
    content=content,
    tool_calls=response.tool_calls,
    messages=messages,
    handler=handler,
    memory_context=memory_context,
)

if interception_status == INTERCEPTED:
    continue  # 异步路径：LLM 重跑（messages 已 append assistant + user）
if interception_status == INTERCEPTED_SYNC:
    # 同步路径：yield wrapped_text + 显式 return
    yield StreamEvent("reply", interception_payload)
    # 子 Agent 路径不调全局 clear_stop()（避免清主 Agent stop 标志）
    yield StreamEvent("system", "chat_idle")
    return {"result": "INTERCEPTED_SYNC", "messages": messages, "finish_reason": "intercepted_sync"}
if interception_status == EXIT:
    stripped_content = content.lstrip()
    if stripped_content.startswith("@end"):
        exit_content = stripped_content[4:].lstrip()
        if not exit_content:
            exit_content = content
    else:
        exit_content = content
    yield StreamEvent("reply", exit_content)
    yield StreamEvent("system", "chat_idle")
    return {"result": "EXITED", "messages": messages, "finish_reason": "exited"}
    # 事实陈述（v12 审查 B-1 修正）：
    # 现有 EXIT 分支（agent_loop.py:578-589）是 yield reply + break，break 后落到 L934-947 退出逻辑：
    #   - L935 on_turn_end 回调（子 Agent 路径未传该回调，无副作用）
    #   - L937 clear_stop()（清主 Agent 全局 stop 标志——子 Agent 路径不应清，已存在瑕疵）
    #   - L938 yield chat_idle
    #   - L942 return {"result": "CURRENT_TASK_DONE", ...}
    # v12 改为显式 return {"result": "EXITED", ...}：
    #   - 跳过 clear_stop()（有意，避免清主 Agent stop 标志）
    #   - 跳过 on_turn_end（子 Agent 路径未传该回调，无副作用）
    #   - 手动补 yield chat_idle（保证状态清理）
    #   - return EXITED 而非 CURRENT_TASK_DONE（让 §5.5 精确判别 @end 退出 vs 同步 @niu-agent 挂起）
    # 现有 STOPPED 分支（L500/668/739）仍调 clear_stop() 是已存在瑕疵，本阶段不修
if interception_status == FORMAT_ERROR:
    _harness_fail_count = 0
    continue
# NO_INTERCEPTION：继续走原有逻辑
```

**关键设计点**：
- INTERCEPTED_SYNC 和 EXIT 都显式 return，不走末尾 MAX_TURNS_EXCEEDED 路径
- return 值的 `result` 字段用专门的 `"INTERCEPTED_SYNC"` / `"EXITED"`，给 §5.5 精确判别
- 子 Agent 路径不调全局 `clear_stop()`（避免清主 Agent stop 标志）

### 4.5 @end 路径 / FORMAT_ERROR 路径

见 §4.4，与异步路径行为一致。

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

`subagent.py:671` 现有结构改为三分支（**判断顺序：answer 最先**）：

```python
# 顶部校验（v9 审查 I2）：在 get_subagent_config 之前
if not task and not answer:
    return "[错误] chat-with-xxx 必须传 task（新任务）或 answer + unique_name（回复子 Agent 问题）"

# 1. 获取子 Agent 提示词 + temperature
agent_config = get_subagent_config(agent_name)
...

if answer is not None and answer_unique_name is not None:
    # 第三分支：回复路径
    instance = SubagentRegistry.get(answer_unique_name)
    if instance is None or getattr(instance, "state", None) != "waiting_for_answer":
        return f"[错误] 找不到挂起的子 Agent session（unique_name={answer_unique_name}），可能已被终止"
    # 校验 agent_type 匹配（v11 审查 B2）：防止主 Agent LLM 把 A 子 Agent 的 unique_name 传给 B 子 Agent 的 chat-with-xxx
    if instance.agent_type != agent_name:
        return f"[错误] unique_name={answer_unique_name} 不属于子 Agent {agent_name}（实际属于 {instance.agent_type}），请检查 unique_name 是否传错"
    
    # 剥除 "@子名 " 前缀（容错：找不到前缀原样使用，记 warning）
    reply_text = _strip_at_prefix(answer, answer_unique_name)
    
    # 注入 user 消息到 suspended_messages
    suspended_messages = instance.suspended_messages
    suspended_messages.append({"role": "user", "content": f"[主 Agent 回答] {reply_text}"})
    
    # 复位 state 为 running（即将重新跑）
    instance.state = "running"
    # 注释（v12 审查 I-2）：不预检查 supplement_queue 是否已有 /stop
    # 时序窗口：state 复位后、_run_agent_loop 启动前，用户可能已 /stop
    # request_stop_all_subagents 看到 state="running" 推 /stop 到 supplement_queue
    # 此时 _run_agent_loop 启动后 agent_runner_loop 内部 L879-894 drain 时检测到 terminate
    # → 走终止总结路径。第三分支不预检查，依赖 agent_runner_loop 内部 drain 检测
    # （若预检查会丢失 /stop 信号，因为 supplement_queue 已 push 但无人 drain）

    # 用 suspended 的全套状态重新调 _run_agent_loop
    try:
        result_text, return_value = _run_agent_loop(
            client=instance.suspended_client,
            system_prompt="",
            system_message=instance.suspended_system_message,
            user_input="",
            initial_user_content=None,  # 跳过 L474-477 user append
            handler=instance.suspended_handler,
            tools_schema=instance.suspended_tools_schema,
            memory_context=None,
            resumed_messages=suspended_messages,
            supplement_queue=instance.supplement_queue,  # 关键：必须传，否则回复路径走全局 drain_supplement 偷主 Agent 消息 + /stop 推到 supplement_queue 时回复路径能检测到
        )
        # §5.5 后处理：在 try 块内、finally 之前调
        _maybe_suspend_session(
            unique_name=answer_unique_name,
            return_value=return_value,
            handler=instance.suspended_handler,
            client=instance.suspended_client,
            tools_schema=instance.suspended_tools_schema,
            system_message=instance.suspended_system_message,
        )
    finally:
        # 条件化 unregister（与同步新任务分支一致）：
        # 若再次 @niu-agent 挂起（state="waiting_for_answer"），跳过 unregister
        final_instance = SubagentRegistry.get(answer_unique_name)
        final_state = getattr(final_instance, "state", None) if final_instance else None
        if final_state != "waiting_for_answer":
            SubagentRegistry.unregister(answer_unique_name)
    
    # 后处理（同 L727-751）：截断/overflow/extract
    ...

elif unique_name is not None:
    # 异步新任务分支（不变）
    ...
else:
    # 同步新任务分支（改动：handler._is_sync_subagent = True）
    ...
```

**关键设计点**：
- 第三分支复位 `instance.state = "running"` 后再跑 _run_agent_loop，让子 Agent 内部若再次 @niu-agent 能重新设 state="waiting_for_answer"
- 第三分支 finally 也条件化 unregister——多轮 @niu-agent 时第二次 call_subagent 不注销，第三次才能正常注销
- **client 复用风险**：`instance.suspended_client` 是第一次创建的 LiteLLM client。LiteLLM client 内部可能有 retry 状态/HTTP 连接池。**实施时实测验证**：若发现 retry 状态干扰，第二次 call_subagent 重新 `create_client(llm_config)` 替代复用

### 5.3 同步新任务分支改动

`subagent.py:696-723` 的同步新任务分支，在 L643 创建 handler 之后加：

```python
handler._is_sync_subagent = True  # 同步路径标记
```

异步新任务分支（L671-695）和异步 _dispatch_async_subagent 路径都要显式设：

```python
handler._is_sync_subagent = False  # 异步路径标记
```

### 5.4 同步新任务分支：后处理 + finally 条件化（时序明确）

`subagent.py:696-723` 当前结构：

```python
try:
    result_text, return_value = _run_agent_loop(...)
finally:
    SubagentRegistry.unregister(unique_name)
# 后处理 L727-751 在 try/finally 之外
```

**改为**（§5.5 后处理移入 try 块内、finally 之前）：

```python
try:
    result_text, return_value = _run_agent_loop(...)
    # §5.5 后处理：必须在 try 块内、finally 之前执行
    _maybe_suspend_session(
        unique_name=unique_name,
        return_value=return_value,
        handler=handler,
        client=client,
        tools_schema=tools_schema,
        system_message=system_message,
    )
finally:
    # 条件化 unregister：state="waiting_for_answer" 时跳过
    instance = SubagentRegistry.get(unique_name)
    state = getattr(instance, "state", None) if instance else None
    if state != "waiting_for_answer":
        SubagentRegistry.unregister(unique_name)
# 后处理 L727-751 的截断/overflow/extract 逻辑仍在 finally 之后
```

### 5.5 `_maybe_suspend_session` helper

```python
def _maybe_suspend_session(unique_name, return_value, handler, client, tools_schema, system_message):
    """检测同步 @niu-agent 挂起信号，存挂起状态到 registry。

    必须在 try 块内、finally 之前调用（修复 v6 BLOCKER B3/B4 时序歧义）。
    异常安全（v7 B1）：全程 try/except 包裹，确保 _run_agent_loop 返回 INTERCEPTED_SYNC 后
    即使 helper 中途抛异常，state 也被强制设为 waiting_for_answer。
    """
    if not (return_value and isinstance(return_value, dict)):
        return
    result_flag = return_value.get("result", "")
    if result_flag != "INTERCEPTED_SYNC":
        return
    if not getattr(handler, "_is_sync_subagent", False):
        return
    try:
        instance = SubagentRegistry.get(unique_name)
        if not instance:
            return
        # 校验 return_value["messages"] 非空且首条是 system
        msgs = return_value.get("messages", [])
        if not msgs or not isinstance(msgs[0], dict) or msgs[0].get("role") != "system":
            logger.error(f"[MaybeSuspend] return_value messages 异常（空或首条非 system），不挂起")
            return
        instance.state = "waiting_for_answer"
        instance.suspended_messages = msgs
        instance.suspended_handler = handler
        instance.suspended_client = client
        instance.suspended_tools_schema = tools_schema
        instance.suspended_system_message = system_message
    except Exception as e:
        logger.error(f"[MaybeSuspend] helper 异常，强制设 state=waiting_for_answer: {e}")
        try:
            instance = SubagentRegistry.get(unique_name)
            if instance:
                instance.state = "waiting_for_answer"
                # 其他字段尽力设置
                if instance.suspended_messages is None:
                    msgs = return_value.get("messages", [])
                    if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system":
                        instance.suspended_messages = msgs
                if instance.suspended_handler is None:
                    instance.suspended_handler = handler
                if instance.suspended_client is None:
                    instance.suspended_client = client
                if instance.suspended_tools_schema is None:
                    instance.suspended_tools_schema = tools_schema
                if instance.suspended_system_message is None:
                    instance.suspended_system_message = system_message
        except Exception as fallback_err:
            logger.error(f"[MaybeSuspend] fallback 也失败: {fallback_err}")
            raise RuntimeError(f"_maybe_suspend_session fallback 失败: {fallback_err}") from fallback_err
```

### 5.6 chat-with-xxx schema 改动

`agent/runner.py:312-393` 的 chat-with-xxx schema 加可选参数：

```python
{
    "name": "chat-with-xxx",
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务描述（回复路径可传空字符串）"},
            "answer": {"type": "string", "description": "回复子 Agent 的 @niu-agent 问题（含 @子名 前缀）"},
            "unique_name": {"type": "string", "description": "子 Agent 唯一名（回复时必填）"},
            "async_mode": {"type": "boolean", ...}  # 已有，allowAsync 时才有
        },
        "required": []  # task 改 optional（回复路径 task=""）
    }
}
```

### 5.7 _call_subagent_gen 透传

`agent/handler.py:943-944` 参数解析扩展：

```python
task = args.get("task", "")
async_mode = args.get("async_mode", False)
answer = args.get("answer")  # 新增
unique_name_arg = args.get("unique_name")  # 新增
```

L998 调 call_subagent 的**完整参数清单**（当前 `handler.py:998-1004` 只传 `agent_name/task/llm_config/mcp_client/history` 5 个参数，本阶段追加 2 个）：

```python
result = call_subagent(
    agent_name=agent_name,
    task=task,
    llm_config=llm_config,
    mcp_client=mcp_client,
    history=_history,
    answer=answer,  # 新增
    answer_unique_name=unique_name_arg if answer else None,  # 新增
)
```

**注意**：当前 handler.py 不传 `supplement_queue` / `memory_context` / `unique_name` / `context_fifo_threshold` / `no_tools`——这些在 call_subagent 内部分别由同步新任务分支、异步分支处理。本阶段不改这个现状，只追加 answer + answer_unique_name。

### 5.8 control_flow_results 集合更新

`agent/subagent.py:285` 当前集合：

```python
control_flow_results = {"CONTEXT_OVERFLOW", "EXITED", "MAX_TURNS_EXCEEDED", "CURRENT_TASK_DONE", "TERMINATED_BY_SUPPLEMENT"}
```

**变更**（3 项）：

```python
control_flow_results = {
    "CURRENT_TASK_DONE", "MAX_TURNS_EXCEEDED", "CONTEXT_OVERFLOW",
    "TERMINATED_BY_SUPPLEMENT",
    "STOPPED",           # 顺便补：子 Agent 收到 /stop 终止（agent_loop.py:502/668/739 用此值，当前漏在集合外，已存在 bug）
    "INTERCEPTED_SYNC",  # 新增：同步 @niu-agent 挂起
    "EXITED",            # 已存在：@end 退出（保留，不重复添加）
}
```

- `STOPPED`：当前集合不含，本阶段顺便补（修已存在 bug）
- `INTERCEPTED_SYNC`：当前集合不含，本阶段新增
- `EXITED`：当前集合已含（`subagent.py:285` 验证），但 `agent_loop.py:805-809` 的 EXITED 返回路径是死代码（`should_exit` 从未被赋值为 dict）——现有 @end 路径实际 return `CURRENT_TASK_DONE`（break 后落到 L942）。v11/v12 §4.4 让 @end 路径首次实际 return `EXITED`，激活该占位。**不是"已存在保留"，是"死代码复活"**

---

## 6. _run_agent_loop 与 agent_runner_loop 改造

### 6.1 _run_agent_loop 新增 resumed_messages 参数

`agent/subagent.py:189-204` 的 `_run_agent_loop` 签名加：

```python
def _run_agent_loop(
    ...,
    resumed_messages: Optional[list] = None,
) -> tuple:
```

L228 调 agent_runner_loop 时透传：

```python
gen = agent_runner_loop(..., resumed_messages=resumed_messages)
```

### 6.2 agent_runner_loop 新增 resumed_messages 参数

`agent/generic/agent_loop.py:391-410` 的 `agent_runner_loop` 签名加：

```python
def agent_runner_loop(
    ...,
    resumed_messages: Optional[list] = None,
):
```

L416-477 的 messages 构造逻辑加分支：

```python
if resumed_messages is not None:
    # 回复路径：直接用挂起的 messages，跳过 system_message + history + user_input 构造
    messages = resumed_messages
else:
    messages = [system_message]
    # ... 现有 L416-477 history 处理 + user_input append ...

# === L482+ 的初始化保留在 if/else 之外（对所有路径执行） ===
# turn = 0
# last_prompt_tokens = 0
# handler._last_prompt_tokens = 0
# _compress_cooldown = False
# handler._done_hooks = []
# handler.max_turns = max_turns
# _harness_fail_count = 0
# warning_threshold = _read_warning_threshold()
# yield StreamEvent("system", "chat_busy")
```

**FIFO 裁剪保护**：resumed_messages 路径下，messages[0] 是 system_message（第一次跑 L416 加进去的）。`_fifo_prune`（`agent_loop.py:246-292`）当前保护 messages[0]+messages[1]，从 i=2 开始删。resumed_messages 路径下 messages[1] 不是"初始 user"。**改动**：

```python
# DEFAULT_PROTECT_RECENT_COUNT 值（与 subagent.py:140 现有常量一致，v11 审查 I2）
DEFAULT_PROTECT_RECENT_COUNT = 10

def _fifo_prune(messages, target_tokens, protect_recent_count=DEFAULT_PROTECT_RECENT_COUNT, is_resumed=False):
    if len(messages) <= 2:
        return 0
    if is_resumed:
        protect_end = max(2, len(messages) - protect_recent_count)
    else:
        protect_end = 2
    # 从 protect_end 之前删除（FIFO）
    ...
```

agent_runner_loop 调 `_fifo_prune` 时传 `is_resumed=(resumed_messages is not None)`。

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
    # 新增字段（同步 @niu-agent 挂起状态）
    state: str = "running"  # "running" / "waiting_for_answer"
    suspended_messages: Optional[list] = None
    suspended_handler: Optional[Any] = None
    suspended_client: Optional[Any] = None
    suspended_tools_schema: Optional[list] = None
    suspended_system_message: Optional[dict] = None
```

### 7.2 内存管理

- 挂起状态全内存（与现有 registry 一致），重启即丢失
- 主 Agent 不回复的孤儿 session 由 `/stop` 清理（§8.1）+ 主 Agent 工具循环退出清理（§8.2）
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
            pending_ask.cancel_pending_ask(instance.unique_name)  # 对 sync 是 no-op，安全
            instance.supplement_queue.push("/stop", is_terminate=True)
```

### 8.2 主 Agent 工具循环退出时清理

主 Agent 工具循环可能因 LLM 不调第二次 chat-with-xxx 而退出（如 LLM 直接回用户）——此时挂起的同步 session 残留。

- **清理时机**：主 Agent `agent_runner_loop` 生成器结束（正常 StopIteration、is_stop_requested break、异常）时
- **实现位置**：`agent/runner.py` 的 `NiuRunner` 主 Agent 工具循环生成器的 `finally` 块（L2184-2201 附近，实施时 grep `finally` 在 runner.py 主 Agent 路径定位）
- **同步子 Agent 挂起时主 Agent 工具循环状态**：call_subagent 第一次返回 @niu-agent 问题后，主 Agent dispatch 返回，主 Agent agent_runner_loop 继续跑——此时主 Agent 不在 dispatch 阻塞，可能不调第二次 chat-with-xxx 直接回用户 → agent_runner_loop 正常结束 → finally 块清理挂起 session
- 加 `cleanup_suspended_sync_subagents()` helper 函数，遍历 `SubagentRegistry.list_running()` 注销符合条件的 session

---

## 9. 提示词守则注入（所有子 Agent）

### 9.1 守则模板

`agent/subagent.py` 重新引入守则常量（之前 commit `0ee5660f` 删除的，文案改为 @niu-agent/@end）：

```python
_SUBAGENT_ASK_GUIDE_TEMPLATE = """
## 子 Agent 与主 Agent 对话规则

你是子 Agent，工作未完成时遇到必须澄清的问题，必须用 `@niu-agent ` 前缀的 content 询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。

只有以下情况才能直接返回：
1. 任务已完成，用 `@end ` 前缀返回最终结果。
2. 任务确实无法继续（如缺权限、缺资源），用 `@end ` 前缀汇报情况让主 Agent 决策。

其他任何"需要更多信息才能继续"的情况，一律用 `@niu-agent ` 前缀询问。

格式示例：
- 询问：`@niu-agent 我应该选择哪个选项？`
- 结束：`@end 任务已完成，结果：...`

注：你不需要在输出里包含自己的标识符，程序会自动在你的问题前加上唯一标识，主 Agent 据此回复你。
"""

_SUBAGENT_ASK_GUIDE_MARKER = "<!-- NIU_SUBAGENT_GUIDE_v1 -->"
```

### 9.2 注入逻辑

`build_subagent_system_segments(agent_name)` 不再加 `allow_async` 参数，统一注入：

```python
if _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
    static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
```

---

## 9A. 全仓 @niu 改名为 @niu-agent

### 9A.1 改名动机

`niu` 是知识图谱的根节点，根节点不允许建立连接。但内容提取 Agent（entity-extractor）看到上下文中有旧前缀 `@niu`（无 -agent 后缀）出现时，可能误把对话消息直接连接到知识图谱根节点上。为避免误连，把旧前缀 `@niu` 改名为 `@niu-agent`，让根节点命名空间（`niu`）与子 Agent 询问前缀（`@niu-agent`）显式分离。下文凡涉及"改名前"的旧字符串用 `@niu`（无 -agent 后缀），"改名后"的新字符串用 `@niu-agent`。

### 9A.2 改名范围

全仓所有旧前缀 `@niu`（无 -agent 后缀）字符串改为新前缀 `@niu-agent`，包括前几个阶段已完成的功能（阶段二异步路径 + 阶段三@前缀方案）。已知 13 个文件 ~179 处需改（不含 logs/raw_http/ 运行时产物）：

| 类别 | 文件 | 改动 |
|------|------|------|
| 拦截层代码 | `agent/generic/agent_loop.py` | 11 处：L13/54/74/75/76/77/79/84/97/126/577。**关键**：L77 `stripped[4:]` 改为 `stripped[len("@niu-agent"):]`（10 字符）；L75 `startswith("@niu")` 改为 `startswith("@niu-agent")` 严格匹配；L84/97/126 FORMAT_ERROR 注入文本改 `@niu-agent` |
| 守则注入 | `agent/subagent.py` | §9.1 守则模板用 `@niu-agent` |
| 提示词 | `config/agents/niu.md` | L255/L283/L291 旧 `@niu` 改 `@niu-agent` |
| 提示词 | `config/agent-template.md` | L27/L70 旧 `@niu` 改 `@niu-agent` |
| 测试 | `tests/test_at_prefix_interception.py` | L60/83/98/165/207/215/237 旧 `@niu` 改 `@niu-agent`（测试输入字符串 + 断言） |
| 测试 | `tests/test_ask_main_agent_stop_deadlock.py` | L1/4/36/65/71/85/97/115/128/138 旧 `@niu` 改 `@niu-agent`（含 L71 测试输入，改名后为 `"@niu-agent 这个 PDF 是扫描件吗？"`） |
| 测试 | `tests/verify_llm_at_prefix.py` | L1/22/26/30/105/112/113 旧 `@niu` 改 `@niu-agent` |
| 测试 | `tests/test_ask_main_agent.py` | L95/180/182/183 旧 `@niu` 改 `@niu-agent` |
| 测试 | `tests/test_request_stop_all_subagents.py` | L1/10 旧 `@niu` 改 `@niu-agent` |
| 测试 | `tests/test_db_monitor.py` | L39 旧 `@niu` 改 `@niu-agent` |
| 文档 | `docs/SYSTEM_MANUAL.md` | L348 旧 `@niu` 改 `@niu-agent` |
| 文档 | `docs/manual-general-subagent.md` | L17/86/117 旧 `@niu` 改 `@niu-agent` |
| 设计文档 | `docs/superpowers/specs/2026-07-04-at-prefix-subagent-intent.md` | 61 处旧 `@niu` 改 `@niu-agent`（历史 spec，保持准确） |
| 设计文档 | 本 spec | 65 处旧 `@niu` 改 `@niu-agent`（本文件 §3-§11 已用 `@niu-agent`，§9A 章节保留"改名前"的旧前缀 `@niu` 字面量用于描述） |

**不需改动**：
- `ui/assistant/`（前端无旧 `@niu`）
- `mcp-servers/`（13 个子服务无旧 `@niu`）
- `niu_api/*.py`（API 层无旧 `@niu`）
- `logs/raw_http/`（运行时抓包，历史产物不改）
- `config/agents/niu.md` 文件名（`niu` 是 agent 名，不是 `@niu` 前缀）

### 9A.3 关键风险点

1. **L77 `stripped[4:]` 硬编码长度**：旧 `@niu` 是 4 字符，新 `@niu-agent` 是 10 字符，必须改为 `stripped[len("@niu-agent"):]`。**最易遗漏的拦截层 bug**——剥前缀错会留下 `agent问题xxx` 残片导致 LLM 反复触发 FORMAT_ERROR 死循环。
2. **L75 `startswith("@niu")`**：改为 `startswith("@niu-agent")` 严格匹配。建议**严格匹配**，旧前缀 `@niu` 走 FORMAT_ERROR 让 LLM 学新前缀。
3. **FORMAT_ERROR 注入文本**（L84/97/126）：必须与提示词（niu.md / agent-template.md）措辞一致。
4. **测试输入字符串**：改名后必须同步更新，否则拦截层不识别会 fail。
5. **常量定义**：建议在 `agent_loop.py` 顶部加 `_AT_NIU_PREFIX = "@niu-agent"` 常量，统一引用。
6. **db_monitor 链路 B**：**已确认安全**——`at_message_parser.py:12` 正则 `_AT_PATTERN = re.compile(r'@([a-z]+(?:-[a-z]+)*-[0-9a-f]{4})\s+...')` 要求 4 位 hex 后缀，`@niu-agent` 不匹配，db_monitor 不会误路由。主 Agent 回复格式是 `@<子名-4hex>`（如 `@xxx-ab12`），不会出现 `@niu-agent` 作为回复目标。

### 9A.4 实施顺序

1. `agent/generic/agent_loop.py`：加 `_AT_NIU_PREFIX` 常量 + 改 L75/77/84/97/126 + 注释
2. `agent/subagent.py`：守则模板用 `@niu-agent`（§9.1）
3. `config/agents/niu.md` + `config/agent-template.md`：提示词改 `@niu-agent`
4. `tests/`：6 个测试文件改 `@niu-agent`
5. `docs/`：4 个文档改 `@niu-agent`
6. `agent/db_monitor.py` / `at_message_parser`：验证 `@niu-agent` 路由不误伤（已确认安全）
7. `niu_api/`：grep 确认无旧 `@niu` 前缀（v11 审查 I4）
8. 端到端验证：异步路径 + 同步路径 @niu-agent 询问全流程
9. 知识图谱回归：确认 entity-extractor 不再把 `@niu-agent` 上下文误连到根节点

### 9A.5 验收标准

- 全仓 grep 旧前缀 `@niu`（无 -agent 后缀）仅在以下位置残留：
  - `logs/raw_http/` 历史产物（运行时新日志应为 `@niu-agent`）
  - 本 spec §9A 章节的描述性文本（说明"改名前"的旧字符串，合理保留）
- 代码（agent/ + niu_api/ + mcp-servers/）+ 测试（tests/）+ 提示词（config/agents/*.md + agent-template.md）+ 文档（docs/SYSTEM_MANUAL.md + docs/manual-general-subagent.md + 历史 spec）中无旧前缀 `@niu`
- 所有单元测试通过（含改名后的拦截测试，测试输入用 `@niu-agent`）
- 端到端测试通过（异步 + 同步路径用 `@niu-agent` 前缀）
- 知识图谱验证：entity-extractor 处理含 `@niu-agent` 的对话时不创建到根节点 `niu` 的连接

---

## 9B. niu.md 提示词增量（同步子 Agent 交互关键依赖）

### 9B.1 动机

整个同步路径依赖主 Agent LLM 看到 `[子名] 问题` 工具结果后，正确调 `chat-with-xxx(task="", answer="@子名 回答", unique_name="子名")`。但 §5.6 schema 改 `task` 为 optional + 新增 `answer` / `unique_name` 可选参数后，LLM 有多条岔路：

- ❌ 调 `chat-with-xxx(task="@xxx-ab12 选 A")`（把回答塞进 task）→ 走同步新任务分支，task="@xxx-ab12 选 A" 作为新任务
- ❌ 调 `chat-with-xxx(answer="选 A")` 不传 unique_name → 第三分支条件不成立 → 走同步新任务分支
- ❌ 调 `chat-with-xxx(task="继续做", answer="@xxx-ab12 选 A", unique_name="xxx-ab12")`（同时传 task 和 answer）→ 第三分支优先，task 被忽略
- ✅ 调 `chat-with-xxx(task="", answer="@xxx-ab12 选 A", unique_name="xxx-ab12")` → 正确走回复路径

必须靠 niu.md 提示词约束 LLM 选对路径。**这是 P0 阻塞依赖**。

### 9B.2 niu.md 增量提示词片段

**重要**：主 Agent LLM 看到的工具结果不是纯文本，而是 JSON 包装。`_call_subagent_gen`（`handler.py:1051`）返回 `StepOutcome({"status":"success","result":"..."})`，`agent_loop.py:816-825` 把它 `json.dumps` 序列化为 tool 消息 content。主 Agent LLM 看到的格式是 `{"status":"success","result":"[xxx-ab12] 我该选哪个？"}` JSON 字符串。

在 `config/agents/niu.md` 的"### 收到 [子名] 问题消息时"段（L255 附近）追加：

```markdown
### 收到同步子 Agent @niu-agent 问题（工具结果是 JSON 含 [子名] 问题）

当你调 chat-with-xxx 工具收到的结果文本是 JSON 字符串（如 `{"status":"success","result":"[xxx-ab12] 我该选哪个？"}`），需先在脑内 JSON 解析再取 `result` 字段。`result` 字段含方括号子 Agent 唯一名 + 问题内容时，说明同步子 Agent 在向你提问。你必须：

1. 从 JSON 的 `result` 字段提取问题文本（如 `[xxx-ab12] 我该选哪个？`）
2. 用同一工具名 chat-with-xxx 回复（不要换其他工具）
3. 参数严格按以下格式：
   - `task`：传空字符串 `""`（不要把回答塞进 task）
   - `answer`：传 `@<子名> 你的回答`（含 @子名 前缀，如 `@xxx-ab12 选 A`）
   - `unique_name`：传方括号里的子名（如 `xxx-ab12`）
4. 不要同时传 task 和 answer——task 是新任务，answer 是回复子 Agent 问题，二者互斥

**反例**（禁止）：
- `chat-with-xxx(task="@xxx-ab12 选 A")` — 回答塞进 task，会被当新任务
- `chat-with-xxx(answer="选 A")` — 不传 unique_name，找不到挂起 session
- `chat-with-xxx(task="继续", answer="@xxx-ab12 选 A", unique_name="xxx-ab12")` — task 和 answer 同时传，task 被忽略但语义混乱

**正例**：
- `chat-with-xxx(task="", answer="@xxx-ab12 选 A", unique_name="xxx-ab12")`

同步子 Agent 收到你的回答后会继续工作，可能再次 @niu-agent 提问（你会再收到 JSON result 字段含 `[xxx-ab12] 新问题`），或 @end 结束返回最终结果（result 字段是最终文本，不含方括号）。
```

### 9B.3 agent-template.md 增量

在 `config/agent-template.md` 的"## 提示词正文"段（L27 附近）追加：

```markdown
- **何时主动询问主 Agent**：所有子 Agent（同步 + 异步）都被程序注入 @niu-agent/@end 守则。子 Agent 用 `@niu-agent ` 前缀询问主 Agent，用 `@end ` 前缀结束会话。子 Agent 不需要知道自己的 unique_name，程序会自动包装。
```

### 9B.4 LLM 行为验证

实施完成后，用 `tests/verify_llm_at_prefix.py` 改名后的脚本验证：
- 子 Agent LLM 能正确输出 `@niu-agent 问题`（不是 `@niu`）
- 主 Agent LLM 看到 JSON 工具结果含 `[子名] 问题` 后能正确调 `chat-with-xxx(task="", answer="@子名 回答", unique_name="子名")`

### 9B.5 文件清单 + 优先级

§12 文件清单里 `config/agents/niu.md` 和 `config/agent-template.md` 改动**从 P2 升为 P0**（关键阻塞依赖）。

---

## 10. 错误处理与边界情况

### 10.1 session 挂起后主 Agent 不调第二次 chat-with-xxx

- 主 Agent LLM 看到 `[子名] 问题` 工具结果后可能不调 chat-with-xxx 回复（LLM 决定不回复、上下文超长被压缩、用户中途 /stop）
- 处理：`/stop`（§8.1）+ 主 Agent 工具循环退出（§8.2）兜底清理

### 10.2 主 Agent 回复时 unique_name 不匹配

- call_subagent 第三分支检测 `instance is None or state != "waiting_for_answer"` → 返回错误文本
- 不抛异常（避免中断主 Agent 工具循环）
- 主 Agent LLM 看到错误文本后自行决策

### 10.3 answer 文本不含 @子名 前缀

- `_strip_at_prefix` 找不到前缀时记 warning，原样使用 answer 文本
- 不阻塞流程

### 10.4 嵌套子 Agent（A 调 B，B @niu-agent）

- 当前阶段不处理——子 Agent 调子 Agent 的路径在 chat-with-* 工具过滤时已被移除（`subagent.py:544`）
- 子 Agent 不能再调子 Agent，无嵌套场景

### 10.5 拦截层条件改动后的兼容性

- 主 Agent 路径（`_is_sync_subagent=False` + `memory_context=None`）→ NO_INTERCEPTION
- 异步路径（`memory_context is not None`）→ 异步分支不变
- 同步子 Agent（`memory_context=None` + `_is_sync_subagent=True`）→ 进入拦截层

### 10.6 同步子 Agent 走 @end 时的退出语义

- 拦截层返回 (EXIT, None) → agent_runner_loop yield reply + 显式 return `{"result": "EXITED", ...}`
- call_subagent 后处理：§5.5 检测 result_flag == "INTERCEPTED_SYNC" 不成立（是 EXITED）→ 不存挂起
- `_extract_result_from_return_value` 对 EXITED 返回 None（§5.8 集合含 EXITED）→ call_subagent 返回 result_text
- finally 块 state != "waiting_for_answer" → unregister 清理 session

### 10.7 多轮 @niu-agent token 累积

- 多轮 @niu-agent 下 messages 引用一直是同一个 list，状态正确累积
- `_fifo_prune` 会裁剪（§6.2 改动后用 `protect_recent_count` 保护最近 N 条 + messages[0] system）
- 若 token 超限，`_fifo_prune` 触发裁剪到 target_tokens

---

## 11. 测试策略

### 11.1 单元测试（mock LLM，验证拦截层和路由逻辑）

1. **拦截层条件改动回归**
   - 同步子 Agent（`_is_sync_subagent=True`, `memory_context=None`）输出 `@niu-agent 问题` → 返回 `(INTERCEPTED_SYNC, wrapped_text)`，messages 末尾是 assistant
   - 同步子 Agent 输出 `@end 结果` → 返回 `(EXIT, None)`
   - 同步子 Agent 输出无 `@` 前缀 → 返回 `(FORMAT_ERROR, None)`
   - 主 Agent 路径输出任何 content → 返回 `(NO_INTERCEPTION, None)`（回归）
   - 异步子 Agent 输出 `@niu-agent 问题` → 返回 `(INTERCEPTED, None)`（回归）

2. **`_ask_main_agent_impl_sync` 函数**
   - 调用后：messages 末尾 append assistant content
   - 返回文本格式 `[unique_name] question`
   - 不推 MainAgentRequestQueue

3. **call_subagent 三路入口**
   - 无 answer + 无 unique_name → 同步新任务路径
   - 无 answer + 有 unique_name → 异步新任务路径
   - 有 answer + 有 answer_unique_name → 回复路径
   - 回复路径 instance 不存在 → 返回错误文本，不抛异常
   - 回复路径 state != "waiting_for_answer" → 返回错误文本
   - 多轮 @niu-agent：回复路径跑完后再次 @niu-agent → state 重新设为 "waiting_for_answer" → finally 跳过 unregister → 第三次仍能拿回

4. **finally unregister 条件化**
   - state="waiting_for_answer" → 跳过 unregister
   - state="running" → 正常 unregister
   - 第三分支 finally 也条件化

5. **chat-with-xxx schema 改动**
   - schema 含可选 answer + unique_name 参数
   - task 改为 optional
   - call_subagent 顶部校验 `not task and not answer` → 返回错误文本

6. **`call_subagent_with_auto_answer` helper**
   - 第一次返回正常文本（非 @niu-agent）→ 直接返回
   - 第一次返回 @niu-agent 问题 → 自动回复 → 第二次返回 @end → 返回最终结果
   - 多轮 @niu-agent → 多轮自动回复
   - 子 Agent 正常结果含 `[已完成] 文件 X` → 不误判（精确正则匹配）

7. **resumed_messages 参数**
   - agent_runner_loop 收到 resumed_messages → 跳过 messages 构造
   - L482+ 初始化仍执行（在 if/else 之外）
   - _fifo_prune 用 is_resumed=True 走 protect_recent_count 保护

8. **提示词注入**
   - 所有子 Agent（同步 + 异步）build_subagent_system_segments 都注入守则段
   - 去重 marker 是 `<!-- NIU_SUBAGENT_GUIDE_v1 -->`
   - 子 Agent 正文已含 marker 时不重复注入

9. **request_stop_all_subagents 改造**
   - state="waiting_for_answer" → 直接 unregister
   - state="running" → 推 /stop 终止

10. **control_flow_results 集合**
    - 含 INTERCEPTED_SYNC 和 STOPPED
    - _extract_result_from_return_value 对这俩返回 None
    - call_subagent 用 result_text

11. **agent_runner_loop 显式 return**
    - INTERCEPTED_SYNC 分支 yield reply + return `{"result": "INTERCEPTED_SYNC", ...}`
    - EXIT 分支 yield reply + return `{"result": "EXITED", ...}`
    - 不走末尾 MAX_TURNS_EXCEEDED 路径

12. **异常路径测试**
    - 回复路径 _run_agent_loop 抛 LLM 异常 → finally 仍执行条件化 unregister → session 不残留
    - 主 Agent 工具循环异常退出 → runner finally 块调 cleanup_suspended_sync_subagents → 挂起 session 被清理
    - 回复路径 unique_name 存在但 state="running" → 返回错误文本，不抛异常

13. **/stop 在回复路径生效**
    - 第二次 call_subagent 跑过程中用户 /stop → request_stop_all_subagents 推 /stop 到 supplement_queue → 回复路径 agent_runner_loop 检测到 terminate → 走终止总结路径
    - 验证：必须传 supplement_queue=instance.supplement_queue 给 _run_agent_loop

14. **STOPPED 控制流修复**
    - 子 Agent 收到 /stop 终止 → return `{"result": "STOPPED", ...}` → extract 返回 None → call_subagent 返回 result_text 而非 JSON dump

15. **主 Agent 不调第二次 chat-with-xxx**
    - mock 主 Agent LLM 收到 `[子名] 问题` 工具结果后直接回用户文本 → 主 Agent 工具循环退出 → runner finally 块调 cleanup_suspended_sync_subagents → registry 无残留

16. **第二次跑过程中 MAX_TURNS_EXCEEDED**
    - 第二次 call_subagent 跑过程中子 Agent 跑满 max_turns → agent_runner_loop return `{"result": "MAX_TURNS_EXCEEDED", ...}` → §5.5 helper 检测 result_flag != INTERCEPTED_SYNC → 不挂起 → finally state="running" → unregister → 主 Agent 第三次调 chat-with-xxx 拿不回 → 返回错误文本

17. **主 Agent LLM 误用反例验证（v12 审查 B-3：从 §11.2 移到 §11.1 单元测试，允许 mock LLM）**
    - mock 主 Agent LLM 把回答塞进 task（调 chat-with-xxx(task="@xxx-ab12 选 A")）→ call_subagent 顶部校验拦截 → 返回错误文本
    - mock 主 Agent LLM 不传 unique_name（调 chat-with-xxx(answer="选 A")）→ 第三分支条件不成立走同步新任务分支 → task="" → call_subagent 顶部校验拦截
    - mock 主 Agent LLM 把 A 子 Agent unique_name 传给 B 子 Agent chat-with-xxx → 第三分支 agent_type 校验拦截 → 返回错误文本（v12 审查 I-1）
    - 验证：call_subagent 顶部校验 + agent_type 校验函数返回错误文本（不验证主 Agent LLM 纠错行为，那部分移到 §11.2 端到端测试 8）

### 11.2 端到端测试（真实 LLM + 真实程序，禁 mock）

1. **同步子 Agent @niu-agent 询问 + 主 Agent 回复 + 子 Agent 继续**
   - 主 Agent 调 chat-with-xxx → 子 Agent @niu-agent 问澄清问题 → 主 Agent 看到 JSON 工具结果含 `[子名] 问题` → 回 `@子名 回答` → 子 Agent 收到回答继续工作 → @end 返回结果
   - 验证方法：检查日志 `[AgentLoop]` + `chat-with-xxx` 出现两次（第一次 task 调用 + 第二次 answer 调用）；检查最终回复不含纯文本 fallback（即主 Agent 没有绕过工具循环直接回用户）；检查 SubagentRegistry 在 @end 后无残留 session（v12 审查 I-3 修正观察方法）

2. **同步子 Agent 多轮 @niu-agent**
   - 子 Agent 连续问 3 次 @niu-agent → 主 Agent 回复 3 次 → 子 Agent @end

3. **同步子 Agent @end 直接结束**
   - 子 Agent 不问问题直接 @end → 主 Agent 收到结果 → 工具循环结束

4. **同步子 Agent 格式错误回退**
   - 子 Agent 第一次输出无 @ 前缀 → 触发 FORMAT_ERROR → 第二次输出 @niu-agent

5. **程序触发子 Agent @niu-agent 自动回复**
   - 触发 auto_tidy → 子 Agent @niu-agent → 自动回复固定文案 → 子 Agent @end
   - 验证：固定文案正确送回；不阻塞主 Agent

6. **/stop 终止挂起的同步子 Agent**
   - 同步子 Agent @niu-agent 挂起 → 用户 /stop → registry 清理挂起 session
   - 验证：session 不残留；主 Agent 工具循环正确退出

7. **回归测试**
   - 异步子 Agent 所有行为不变（5 次 @niu-agent + @end + 格式错误 + /stop）
   - 主 Agent 正常对话不被拦截层误伤

8. **主 Agent LLM 真实纠错行为（v13 审查 I-1：从 §11.1 移到端到端测试）**
   - 构造一个会让主 Agent LLM 误用 task 字段塞回答的场景（如 niu.md 提示词故意不约束 answer 参数）→ 真实 LLM 调 chat-with-xxx(task="@子名 回答") → call_subagent 顶部校验返回错误文本 → 真实 LLM 看到错误文本后改用正确格式 chat-with-xxx(task="", answer=..., unique_name=...) → 子 Agent 收到回答继续工作
   - 验证：真实 LLM 能从错误文本中纠正为正确格式（不是 mock 编程的纠错）

---

## 12. 修改的文件清单

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `agent/subagent_registry.py` | RunningSubagent 新增 6 字段（state / suspended_*） | P0 |
| `agent/generic/agent_loop.py` | 拦截条件加 `is_sync_subagent`；@niu-agent 同步分支调 `_ask_main_agent_impl_sync`；新增 INTERCEPTED_SYNC 常量 + agent_runner_loop 处理分支；新增 `resumed_messages` 参数 + 跳过 messages 构造；**拦截层返回值改 tuple，现有 L576/L578/L590 `==` 比较改 `interception_status ==` 元组首元素**；子 Agent 路径不调全局 `clear_stop()`；§9A 改名：加 `_AT_NIU_PREFIX` 常量 + L75/77/84/97/126 改 `@niu-agent`；`_fifo_prune` 加 `is_resumed` 参数 | P0 |
| `agent/subagent.py` | 重新引入 `_SUBAGENT_ASK_GUIDE_TEMPLATE` / `_SUBAGENT_ASK_GUIDE_MARKER`；`build_subagent_system_segments` 统一注入守则；新增 `_ask_main_agent_impl_sync`；`call_subagent` 加 `answer` + `answer_unique_name` 参数 + 第三分支；同步新任务分支设 `handler._is_sync_subagent=True`；异步分支设 `handler._is_sync_subagent=False`；`_run_agent_loop` 加 `resumed_messages` 参数；call_subagent 后处理存挂起状态；finally unregister 条件化；§5.8 control_flow_results 集合加 INTERCEPTED_SYNC + STOPPED | P0 |
| `agent/handler.py` | `_call_subagent_gen` 解析 answer + unique_name 参数 + 透传给 call_subagent | P0 |
| `agent/runner.py` | chat-with-xxx schema 加 answer + unique_name 可选参数；`request_stop_all_subagents` 加挂起 session 扫描清理；主 Agent 工具循环退出时调 `cleanup_suspended_sync_subagents`；程序触发点（`runner.py:1223`）替换为 `call_subagent_with_auto_answer` | P0-P1 |
| `niu_api/compat.py` | 9 个程序触发点替换为 `call_subagent_with_auto_answer` | P1 |
| `config/agents/niu.md` | L255/L283/L291 同步——同步子 Agent 也会 @niu-agent + §9B 提示词增量 | P0 |
| `config/agent-template.md` | L27/L70 改 `@niu-agent` + §9B.3 增量 | P0 |
| `docs/SYSTEM_MANUAL.md` | 同步子 Agent 交互描述更新 + L348 改 `@niu-agent` | P1 |
| `docs/manual-general-subagent.md` | 通用子 Agent 手册更新 + L17/86/117 改 `@niu-agent` | P1 |
| `docs/superpowers/specs/2026-07-04-at-prefix-subagent-intent.md` | 61 处旧 `@niu` 改 `@niu-agent` | P1 |
| `tests/test_at_prefix_interception.py` | **现有测试断言改 tuple**：所有 `result == X` 改为 `result == (X, None)` 或 `result[0] == X`；加同步路径拦截测试 + INTERCEPTED_SYNC；旧 `@niu` 改 `@niu-agent` | P0 |
| `tests/test_sync_subagent_interaction.py` | 新建——同步子 Agent 交互单元测试 | P0 |
| `tests/test_call_subagent_with_auto_answer.py` | 新建——helper 单元测试 | P1 |
| `tests/test_subagent_registry.py` | 新增字段测试 + state 转换测试 | P0 |
| `tests/test_ask_main_agent_stop_deadlock.py` | 10 处旧 `@niu` 改 `@niu-agent` | P0 |
| `tests/verify_llm_at_prefix.py` | 7 处旧 `@niu` 改 `@niu-agent` | P0 |
| `tests/test_ask_main_agent.py` | 4 处旧 `@niu` 改 `@niu-agent` | P0 |
| `tests/test_request_stop_all_subagents.py` | 2 处旧 `@niu` 改 `@niu-agent` | P0 |
| `tests/test_db_monitor.py` | 1 处旧 `@niu` 改 `@niu-agent` | P0 |

---

## 13. 实施顺序（粗略，详细 plan 由 writing-plans skill 生成）

1. **§9A 全仓改名**（先做，避免后续实施时新旧前缀混淆）：
   - `agent/generic/agent_loop.py` 加 `_AT_NIU_PREFIX` 常量 + L75/77/84/97/126 改 `@niu-agent` + `stripped[4:]` 改 `stripped[len("@niu-agent"):]`
   - `config/agents/niu.md` + `config/agent-template.md` 旧 `@niu` 改 `@niu-agent`
   - 6 个测试文件旧 `@niu` 改 `@niu-agent`
   - `agent/db_monitor.py` / `at_message_parser` 验证 `@niu-agent` 路由不误伤（已确认安全）
   - 4 个文档旧 `@niu` 改 `@niu-agent`
   - 跑全量测试确认改名无回归
2. SubagentRegistry 字段扩展（state + suspended_*）
3. 守则注入恢复（所有子 Agent 统一注入 @niu-agent/@end 守则）—— 立即解决"第一轮 FORMAT_ERROR"问题
4. 拦截层改造（条件加 is_sync_subagent + 拦截层返回值改 tuple + 新增 INTERCEPTED_SYNC 常量 + 同步 @niu-agent 分支）
5. `_ask_main_agent_impl_sync` 实现
6. agent_runner_loop INTERCEPTED_SYNC 分支 + resumed_messages 参数 + _fifo_prune is_resumed 参数
7. _run_agent_loop resumed_messages 参数
8. call_subagent 第三分支 + 同步新任务分支设 _is_sync_subagent + finally 条件化 unregister + 后处理存挂起状态 + 顶部校验
9. chat-with-xxx schema 改动 + _call_subagent_gen 透传
10. `call_subagent_with_auto_answer` helper 实现
11. 派 Agent 全面排查程序触发点 + 替换为 helper
12. request_stop_all_subagents 改造 + 主 Agent 工具循环退出清理
13. control_flow_results 集合加 INTERCEPTED_SYNC + STOPPED
14. §9B niu.md / agent-template.md 提示词增量
15. 单元测试 + 端到端测试
16. 文档同步
17. 知识图谱回归验证（entity-extractor 不再把 `@niu-agent` 上下文误连到根节点 `niu`）

---

## 14. 验收标准

- 全仓 grep 旧前缀 `@niu`（无 -agent 后缀）仅在 `logs/raw_http/` 历史产物 + 本 spec §9A 描述性文本中残留
- 所有单元测试通过（含同步路径拦截测试 + helper 测试 + schema 测试 + registry 字段测试 + 改名后测试输入用 `@niu-agent`）
- 端到端测试 8 个场景全部通过（真实 LLM，用 `@niu-agent` 前缀）
- 异步路径回归无 bug（5 次 @niu-agent + @end + 格式错误 + /stop）
- 主 Agent 正常对话不被拦截层误伤
- 程序触发子 Agent（auto_tidy / force 压缩 / 手动 tidy）@niu-agent 自动回复不阻塞
- 子 Agent 第一次输出就知道用 @niu-agent 前缀（不再触发 FORMAT_ERROR）
- 知识图谱回归：entity-extractor 处理含 `@niu-agent` 的对话时不创建到根节点 `niu` 的连接
- 代码审查通过（spec 合规 + 代码质量两轮，无 BLOCKER）

---

## 15. 相关文档与提交

- 阶段三 spec：`docs/superpowers/specs/2026-07-04-general-subagent-stage3-design.md`
- 阶段三实施完成提交：`10b5bcf4`
- 阶段三回退守则注入提交：`0ee5660f`
- 阶段四 spec v1-v10 归档：`docs/superpowers/specs/2026-07-05-stage4-sync-subagent-interaction-design.v10-archive.md`
- 阶段四 spec v11 提交：（待生成）

相关记忆：
- [[at-prefix-subagent-intent]] — 阶段三@前缀方案
- [[main-subagent-no-interaction-channel]] — 主子 Agent 交互通道总览
- [[main-subagent-interaction-stage2]] — 阶段二异步路径
- [[stage2-ask-main-agent-stop-deadlock]] — 5 个死锁约束
