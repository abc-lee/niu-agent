# 阶段四：同步子 Agent 交互通道设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让同步调用的子 Agent 也能用 `@niu` / `@end` 前缀表达意图，与主 Agent 对话时底层走 MCP 工具返回值通道（不退出主 Agent 工具循环），消息格式与异步路径完全一致。

**Architecture:** 同步子 Agent 输出 `@niu 问题` 时，agent_runner_loop 拦截层识别后挂起 session（state="waiting_for_answer"）并把问题作为工具返回值送回主 Agent；主 Agent LLM 看到 `[子名] 问题` 后按 niu.md 提示词生成 `@子名 回答`，重调 `chat-with-xxx(answer, unique_name)`；call_subagent 检测到 answer 参数后从 SubagentRegistry 拿回挂起 session，注入回复继续跑。程序触发子 Agent（无主 Agent 在线）由 helper 自动回复固定文案。所有子 Agent（同步+异步）强制注入 @niu/@end 守则段。

**Tech Stack:** Python（agent_loop.py / subagent.py / handler.py / runner.py / compat.py），SQLite（SubagentRegistry 状态），OpenAI tool schema（chat-with-xxx 加可选参数）。

---

## 1. 背景与动机

### 1.1 阶段三遗留问题

阶段三完成了异步子 Agent 的 @前缀交互通道，但同步子 Agent 调用时 `memory_context=None`，被拦截层 NO_INTERCEPTION 跳过——同步子 Agent 仍走旧逻辑：无 tool_calls 即当作会话结束，content 作为工具返回值送回主 Agent，子 Agent 线程退出。

旧逻辑在子 Agent "遇到困难但未明确退出"时出问题：子 Agent 会以"提问文本"作为 content 直接返回，主 Agent 看到的是问题文本但已无子 Agent session 可回复，造成工作未完成。

### 1.2 阶段三实施过程中暴露的次要问题

- 阶段三回退了"结构注入 ask_main_agent 守则"的代码（commit `0ee5660f`），导致子 Agent 第一次输出不知道用 @niu 前缀，会先触发 FORMAT_ERROR 后第二轮才学会。
- 守则注入应同时覆盖同步和异步子 Agent，不能只针对异步。

### 1.3 阶段四目标

- 同步子 Agent 也走 @前缀拦截层，与异步路径行为一致
- 消息格式统一：同步和异步的 @niu 问题都包装成 `[子名] 问题`，主 Agent 回复都包装成 `@子名 回答`
- 传输通道分离：异步走 db_monitor 链路 A→B（主 Agent 闲置触发新一轮），同步走"工具返回值 → 主 Agent 工具循环内回答 → 工具再次调用回送"
- 程序触发子 Agent 由 helper 自动回复固定文案
- 所有子 Agent 强制注入 @niu/@end 守则段

---

## 2. 核心设计原则

1. **消息格式统一**——同步和异步的 @niu 问题都包装成 `[子名] 问题`，主 Agent 回复都包装成 `@子名 回答`。主 Agent LLM 不感知对方是同步还是异步。
2. **传输通道分离**——异步走 db_monitor 链路 A→B（主 Agent 闲置触发新一轮），同步走"工具返回值 → 主 Agent 工具循环内回答 → 工具再次调用回送"。
3. **session 状态存 SubagentRegistry**——同步子 Agent 遇 @niu 时也注册到 registry（复用异步路径基础设施），第二次 call_subagent 调用时拿回挂起的 session。
4. **所有子 Agent 注入 @niu/@end 守则**——不仅异步，同步子 Agent 也强制注入（解决"第一轮不知道用 @niu 触发 FORMAT_ERROR"问题）。
5. **程序触发点包 while 循环**——auto_tidy / force 压缩 / 手动 tidy API 等场景，收到 @niu 自动回复固定文案"无法解答你的问题，请选择 @end 结束并汇报你的工作，或自我抉择选择继续工作"。

---

## 3. 数据流

### 3.1 用户触发同步子 Agent 的完整流程

```
1. 主 Agent LLM 调 chat-with-xxx(task="做 X")
   ↓
2. handler.dispatch → call_subagent(task="做 X", agent_name="xxx")
   ↓
3. call_subagent 检测到无 answer 参数 → 创建新 session：
   - 生成 unique_name = "xxx-ab12"（kebab-case + 4 位 hex）
   - 创建 NiuHandler + LLM client
   - 注册到 SubagentRegistry（state="running", mode="sync"）
   - handler._subagent_unique_name = unique_name
   - handler._is_sync_subagent = True
   ↓
4. 跑 agent_runner_loop（同步路径，memory_context=None 但 is_sync_subagent=True）
   ↓
5. 子 Agent LLM 输出 "@niu 我该选哪个？"
   ↓
6. _intercept_at_prefix_content 拦截：
   - 拦截条件改为：(memory_context is not None) OR (handler._is_sync_subagent is True)
   - 检测到 @niu → 调 _ask_main_agent_impl_sync(question, unique_name)
   ↓
7. _ask_main_agent_impl_sync：
   - 把 session 状态存到 SubagentRegistry（state="waiting_for_answer", suspended_messages=messages, suspended_handler=handler, suspended_client=client）
   - 包装问题为 "[xxx-ab12] 我该选哪个？"
   - 返回这个文本（不阻塞，立即返回）
   ↓
8. agent_runner_loop yield reply "[xxx-ab12] 我该选哪个？" → return CURRENT_TASK_DONE
   ↓
9. call_subagent 收到 StopIteration → 提取 result_text → 返回给主 Agent
   （call_subagent 函数返回，调用栈销毁，但 session 状态已在 registry 里）
   ↓
10. 主 Agent LLM 看到 chat-with-xxx 工具结果 = "[xxx-ab12] 我该选哪个？"
    → 按 niu.md 提示词生成回复 "@xxx-ab12 选 A"
    → 调 chat-with-xxx(answer="@xxx-ab12 选 A", unique_name="xxx-ab12")
    ↓
11. handler.dispatch → call_subagent(answer="...", unique_name="xxx-ab12")
    ↓
12. call_subagent 检测到 answer 参数 + unique_name → 走"回复路径"：
    - 从 SubagentRegistry 拿回 suspended session（messages / handler / client）
    - 剥除 "@xxx-ab12 " 前缀 → "选 A"
    - 注入 user 消息 "[主 Agent 回答] 选 A"
    - continue agent_runner_loop（从中断处继续）
    ↓
13. 子 Agent 继续跑 → 输出 "@end 任务完成" 或再次 "@niu"
    ↓
14. @end 路径：剥前缀 → yield reply "任务完成" → return CURRENT_TASK_DONE
    → call_subagent 收到 StopIteration → 提取 result_text → 从 registry 删除 session → 返回给主 Agent
    → 主 Agent 工具循环结束
```

### 3.2 与异步路径的差异对比

| 维度 | 异步路径 | 同步路径 |
|------|---------|---------|
| memory_context | 非 None | None（但 `handler._is_sync_subagent=True`） |
| @niu 问题送出通道 | push MainAgentRequestQueue → db_monitor 链路 A 检测主 Agent 闲置 → 触发新一轮 LLM | yield reply → 作为工具返回值送主 Agent → 主 Agent 在当前工具循环内回答 |
| 主 Agent 回复送回通道 | db_monitor 链路 B 路由到 SubagentSupplementQueue | 主 Agent 重调 chat-with-xxx(answer, unique_name) → call_subagent 注入 |
| session 状态存储 | SubagentRegistry（mode="async"） | SubagentRegistry（mode="sync"） |
| 消息格式 | `[子名] 问题` / `@子名 回答` | `[子名] 问题` / `@子名 回答`（完全一致） |
| 主 Agent 是否阻塞 | 不阻塞（异步） | 阻塞在 dispatch 工具调用上（同步） |
| 主 Agent 工具循环是否退出 | 退出（异步路径主 Agent 跨多轮） | 不退出（同一轮工具调用内） |

### 3.3 程序触发子 Agent 的特殊处理

程序触发子 Agent（auto_tidy / force 压缩 / 手动 tidy API）时，没有主 Agent 在工具循环里等着。子 Agent 输出 `@niu 问题` 时由 helper 函数自动回复固定文案。

新封装 `call_subagent_with_auto_answer(agent_name, task, ...)`：

```python
def call_subagent_with_auto_answer(agent_name, task, ...):
    """程序触发子 Agent 专用：自动回复 @niu，遇到 @end 或正常文本才返回。"""
    AUTO_ANSWER = "无法解答你的问题，请选择 @end 结束并汇报你的工作，或自我抉择选择继续工作"
    
    result = call_subagent(agent_name, task, ...)
    while _is_at_niu_question(result):
        unique_name = _extract_unique_name(result)
        result = call_subagent(
            agent_name=agent_name,
            task="",
            answer=AUTO_ANSWER,
            unique_name=unique_name,
            ...
        )
    return result
```

**程序触发点需全面排查**——派 Agent 通读全仓所有"不经主 Agent LLM 决策直接调 call_subagent"的位置（包括但不限于 `niu_api/compat.py` 的 auto_tidy 链、`agent/runner.py` 的 force 压缩链、`/api/context/tidy` 手动 API）。所有此类调用点替换为 `call_subagent_with_auto_answer`。

**不防御死循环**——工具循环本身有"3 次同工具同参数提醒"机制，自动回复次数不加上限，不强制终止。

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

主 Agent 路径（`memory_context=None` + `_is_sync_subagent=False`）仍返回 NO_INTERCEPTION——主 Agent 不被拦截。异步路径（`memory_context is not None`）行为不变。同步子 Agent（`memory_context=None` + `_is_sync_subagent=True`）进入拦截层。

### 4.2 @niu 路径分同步/异步

拦截层检测到 `@niu` 时，根据是否同步走不同函数：

```python
if stripped.startswith("@niu"):
    question = stripped[4:].lstrip()
    if not question: return FORMAT_ERROR
    unique_name = getattr(handler, "_subagent_unique_name", "")
    if not unique_name: return FORMAT_ERROR
    
    if is_sync_subagent:
        # 同步路径：不阻塞，包装问题返回给主 Agent
        from agent.subagent import _ask_main_agent_impl_sync
        wrapped = _ask_main_agent_impl_sync(question=question, unique_name=unique_name, handler=handler, messages=messages, client=client)
        # 注入 assistant content（保留对话历史），但不注入 user 回答（同步路径要等主 Agent 重调）
        messages.append({"role": "assistant", "content": content})
        # 用 wrapped 文本作为 yield reply 内容（让 call_subagent 拿到）
        # 通过特殊机制让 agent_runner_loop yield 这个文本后 return
        ...
    else:
        # 异步路径：阻塞等主 Agent 回答（现有逻辑）
        from agent.subagent import _ask_main_agent_impl
        answer = _ask_main_agent_impl(question=question, unique_name=unique_name)
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": f"[主 Agent 回答] {answer}"})
        return INTERCEPTED
```

**关键设计点**：同步路径需要让 agent_runner_loop yield wrapped 文本后立即 return（CURRENT_TASK_DONE），让 call_subagent 拿到结果返回给主 Agent。新增返回值常量 `INTERCEPTED_SYNC`：拦截层同步 @niu 分支返回 `INTERCEPTED_SYNC`；agent_runner_loop 收到后 `yield StreamEvent("reply", wrapped_text)` + `break`（退出 while 循环）+ 走 CURRENT_TASK_DONE return 路径。call_subagent 收到 StopIteration → 提取 result_text = wrapped_text → 返回给主 Agent。同步路径不向 messages 注入 user 回答（回答由主 Agent 重调 call_subagent 时注入）。

### 4.3 @end 路径

同步和异步的 @end 路径行为一致：剥前缀 → yield reply → return。当前 `agent_loop.py:578-589` 的 EXIT 分支已支持，无需改动。

### 4.4 FORMAT_ERROR 路径

同步和异步的格式错误处理一致：追加错误提示 + continue。当前 `agent_loop.py:590-592` 已支持，无需改动。

---

## 5. call_subagent 双路入口

### 5.1 函数签名扩展

`agent/subagent.py:573` 的 `call_subagent` 加两个可选参数：

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
    answer: Optional[str] = None,  # 新增：回复路径用
    answer_unique_name: Optional[str] = None,  # 新增：回复路径用，标识要拿回哪个 session
) -> str:
```

### 5.2 入口分支

```python
if answer is not None and answer_unique_name is not None:
    # 回复路径：从 registry 拿回挂起 session
    suspended = SubagentRegistry.get(answer_unique_name)
    if suspended is None:
        return f"[错误] 找不到挂起的子 Agent session（unique_name={answer_unique_name}），可能已被终止"
    # 剥除 "@子名 " 前缀
    reply_text = _strip_at_prefix(answer, answer_unique_name)
    # 注入 user 消息
    suspended.messages.append({"role": "user", "content": f"[主 Agent 回答] {reply_text}"})
    # continue agent_runner_loop（用 suspended 的 client/handler/tools_schema/messages）
    ...继续跑 _run_agent_loop...
else:
    # 新任务路径：现有逻辑
    ...
```

### 5.3 chat-with-xxx schema 改动

`agent/handler.py` 的 `_call_subagent_gen` 和工具 schema 生成处加可选参数：

```python
{
    "name": "chat-with-xxx",
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务描述"},
            "answer": {"type": "string", "description": "回复子 Agent 的 @niu 问题（含 @子名 前缀）"},
            "unique_name": {"type": "string", "description": "子 Agent 唯一名（回复时必填）"}
        },
        "required": ["task"]  # task 仍必填，回复路径可传空字符串
    }
}
```

主 Agent 看到 `[子名] 问题` 工具结果后，按 niu.md 提示词生成 `@子名 回答`，调 `chat-with-xxx(task="", answer="@子名 回答", unique_name="子名")`。

---

## 6. 提示词守则注入（所有子 Agent）

### 6.1 守则模板

`agent/subagent.py` 重新引入守则常量（之前 commit `0ee5660f` 删除的）：

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

_SUBAGENT_ASK_GUIDE_MARKER = "## 子 Agent 与主 Agent 对话规则"
```

### 6.2 注入逻辑

`build_subagent_system_segments(agent_name)` 不再加 `allow_async` 参数，统一注入：

```python
# 4. 强制注入 @niu/@end 守则（所有子 Agent）
if _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
    static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
```

### 6.3 模板和文档同步

- `config/agent-template.md` L27 简化——主 Agent 写 MD 时不必再强调 allowAsync 与守则的关系（程序统一注入）
- `config/agents/niu.md` L255/L283/L291 同步——同步子 Agent 也会 @niu，主 Agent 处理逻辑一致
- `docs/SYSTEM_MANUAL.md` + `docs/manual-general-subagent.md` 同步更新

---

## 7. 错误处理与边界情况

### 7.1 session 挂起后主 Agent 不回复

- 子 Agent session 在 registry 里 state="waiting_for_answer"
- 主 Agent 工具循环可能因为各种原因不调回（LLM 决定不回复、上下文超长被压缩、用户中途 /stop）
- 同步路径走工具返回值通道不阻塞，但需要：
  - /stop 终止时：双击停止 / request_stop_all_subagents 时清理挂起的同步 session（与异步同处理）
  - 主 Agent 上下文压缩时：检查 registry 里的挂起 session，要么强制终止要么注入"上下文已压缩"提示

### 7.2 主 Agent 回复时 unique_name 不匹配

- call_subagent 检测 answer 参数有但 unique_name 在 registry 找不到 → 返回错误文本"找不到挂起的子 Agent session（unique_name=xxx），可能已被终止"
- 不抛异常（抛异常会中断主 Agent 工具循环）

### 7.3 session 状态在 registry 里的清理时机

- @end 正常退出 → 立即从 registry 删除
- 主 Agent 工具循环异常退出 → 由 SubagentRegistry 的现有清理机制兜底（同步子 Agent 注册时也要设超时）
- /stop → 清理所有挂起的同步 session（与异步路径同处理）

### 7.4 拦截层条件改动后的兼容性

- 主 Agent 路径（`_is_subagent=False`）仍返回 NO_INTERCEPTION——主 Agent 不应被拦截
- 异步路径（`memory_context is not None`）行为不变
- 同步子 Agent 走拦截层时：@niu → 走 `_ask_main_agent_impl_sync`；@end → 剥前缀 + return；FORMAT_ERROR → 追加错误提示 + continue

### 7.5 同步子 Agent 走 @end 时的退出语义

- 拦截层返回 EXIT → agent_runner_loop yield reply + return CURRENT_TASK_DONE
- call_subagent 收到 StopIteration → 提取 result_text → 从 registry 删除 session → 返回给主 Agent
- 主 Agent 看到工具结果 = "@end 后的内容"，作为最终结果注入 LLM

---

## 8. 测试策略

### 8.1 单元测试（mock LLM，验证拦截层和路由逻辑）

1. **拦截层条件改动回归**
   - 同步子 Agent（`_is_sync_subagent=True`, `memory_context=None`）输出 `@niu 问题` → 触发 `_ask_main_agent_impl_sync` → 返回新常量（如 `INTERCEPTED_SYNC`）
   - 同步子 Agent 输出 `@end 结果` → 返回 EXIT
   - 同步子 Agent 输出无 `@` 前缀 → 返回 FORMAT_ERROR
   - 主 Agent 路径（`_is_subagent=False`, `memory_context=None`）输出任何 content → 返回 NO_INTERCEPTION（回归）
   - 异步子 Agent（`memory_context is not None`）所有行为不变（回归）

2. **`_ask_main_agent_impl_sync` 函数**
   - 调用后：注册到 SubagentRegistry，state="waiting_for_answer"，suspended_messages/handler/client 完整保存
   - 返回文本格式 `[unique_name] question`
   - 不推 MainAgentRequestQueue（与异步路径区别）

3. **call_subagent 双路入口**
   - 无 answer 参数 → 新任务路径：创建 session + 注册 registry + 跑 LLM
   - 有 answer + answer_unique_name → 回复路径：从 registry 拿 session + 注入 user 消息 + continue
   - answer 参数有但 answer_unique_name 在 registry 找不到 → 返回错误文本，不抛异常

4. **chat-with-xxx schema 改动**
   - schema 含可选 answer + unique_name 参数
   - 不带这俩参数 = 新任务调用，正常工作
   - 带这俩参数 = 回复调用

5. **`call_subagent_with_auto_answer` helper**
   - 第一次 call_subagent 返回正常文本（非 @niu）→ 直接返回
   - 第一次返回 @niu 问题 → 自动回复 → 第二次返回 @end → 返回最终结果
   - 多轮 @niu → 多轮自动回复

6. **提示词注入**
   - 所有子 Agent（同步 + 异步）build_subagent_system_segments 都注入守则段
   - 守则段含 `@niu` / `@end` 描述
   - 去重 marker 是 `## 子 Agent 与主 Agent 对话规则`
   - 子 Agent 正文已含 marker 时不重复注入

### 8.2 端到端测试（真实 LLM + 真实程序，禁 mock）

1. **同步子 Agent @niu 询问 + 主 Agent 回复 + 子 Agent 继续**
   - 主 Agent 调 chat-with-xxx → 子 Agent @niu 问澄清问题 → 主 Agent 看到 `[子名] 问题` → 回 `@子名 回答` → 子 Agent 收到回答继续工作 → @end 返回结果
   - 验证：主 Agent 工具循环未退出（同一轮工具调用内）；子 Agent session 在 registry 里正确注册和清理

2. **同步子 Agent 多轮 @niu**
   - 子 Agent 连续问 3 次 @niu → 主 Agent 回复 3 次 → 子 Agent @end
   - 验证：每次 @niu 都正确挂起 + 恢复

3. **同步子 Agent @end 直接结束**
   - 子 Agent 不问问题直接 @end → 主 Agent 收到结果 → 工具循环结束

4. **同步子 Agent 格式错误回退**
   - 子 Agent 第一次输出无 @ 前缀 → 触发 FORMAT_ERROR → 第二次输出 @niu
   - 验证：格式错误提示正确注入

5. **程序触发子 Agent @niu 自动回复**
   - 触发 auto_tidy → 子 Agent @niu 问问题 → 自动回复固定文案 → 子 Agent @end
   - 验证：固定文案正确送回；不阻塞主 Agent

6. **/stop 终止挂起的同步子 Agent**
   - 同步子 Agent @niu 挂起 → 用户 /stop → registry 清理挂起 session
   - 验证：session 不残留；主 Agent 工具循环正确退出

7. **回归测试**
   - 异步子 Agent 所有行为不变（5 次 @niu + @end + 格式错误 + /stop）
   - 主 Agent 正常对话不被拦截层误伤

---

## 9. 修改的文件清单

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `agent/generic/agent_loop.py` | 拦截条件加 `is_sync_subagent` 判断；@niu 同步分支调 `_ask_main_agent_impl_sync`；新增 `INTERCEPTED_SYNC` 等常量 | P0 |
| `agent/subagent.py` | 重新引入 `_SUBAGENT_ASK_GUIDE_TEMPLATE` / `_SUBAGENT_ASK_GUIDE_MARKER`；`build_subagent_system_segments` 统一注入守则；新增 `_ask_main_agent_impl_sync`；`call_subagent` 加 `answer` + `answer_unique_name` 参数 + 回复路径分支；新增 `call_subagent_with_auto_answer` | P0 |
| `agent/handler.py` | `_call_subagent_gen` 透传 answer + answer_unique_name；chat-with-xxx schema 加可选参数 | P0 |
| `agent/runner.py` | 程序触发点（_on_context_high_usage / _run_subagent_step）替换为 `call_subagent_with_auto_answer` | P1 |
| `niu_api/compat.py` | auto_tidy / 手动 tidy API 调用点替换为 `call_subagent_with_auto_answer` | P1 |
| `config/agent-template.md` | L27 简化守则描述（程序统一注入，主 Agent 无需在正文重复） | P2 |
| `config/agents/niu.md` | L255/L283/L291 同步——同步子 Agent 也会 @niu，主 Agent 处理逻辑一致 | P2 |
| `docs/SYSTEM_MANUAL.md` | 同步子 Agent 交互描述更新 | P2 |
| `docs/manual-general-subagent.md` | 通用子 Agent 手册更新 | P2 |
| `tests/test_at_prefix_interception.py` | 加同步路径拦截测试 | P0 |
| `tests/test_sync_subagent_interaction.py` | 新建——同步子 Agent 交互单元测试 | P0 |
| `tests/test_call_subagent_with_auto_answer.py` | 新建——helper 单元测试 | P1 |

---

## 10. 实施顺序（粗略，详细 plan 由 writing-plans skill 生成）

1. 恢复守则注入（所有子 Agent 统一注入 @niu/@end 守则）—— 立即解决"第一轮 FORMAT_ERROR"问题
2. 改造拦截层条件（加 `is_sync_subagent` 判断）+ 新增同步路径 @niu 分支
3. 实现 `_ask_main_agent_impl_sync` + session 挂起机制
4. call_subagent 加 answer + answer_unique_name 参数 + 回复路径
5. chat-with-xxx schema 改动 + handler 透传
6. 实现 `call_subagent_with_auto_answer` helper
7. 派 Agent 全面排查程序触发点 + 替换为 helper
8. 单元测试 + 端到端测试
9. 文档同步（agent-template / niu.md / SYSTEM_MANUAL / manual-general-subagent）

---

## 11. 验收标准

- 所有单元测试通过（含同步路径拦截测试 + helper 测试 + schema 测试）
- 端到端测试 7 个场景全部通过（真实 LLM）
- 异步路径回归无 bug（5 次 @niu + @end + 格式错误 + /stop）
- 主 Agent 正常对话不被拦截层误伤
- 程序触发子 Agent（auto_tidy / force 压缩 / 手动 tidy）@niu 自动回复不阻塞
- 子 Agent 第一次输出就知道用 @niu 前缀（不再触发 FORMAT_ERROR）
- 代码审查通过（spec 合规 + 代码质量两轮）

---

## 12. 相关文档与提交

- 阶段三 spec：`docs/superpowers/specs/2026-07-04-general-subagent-stage3-design.md`
- @前缀方案 spec：`docs/superpowers/specs/2026-07-04-at-prefix-subagent-intent.md`（如有）
- 阶段三实施完成提交：`10b5bcf4`
- 阶段三回退守则注入提交：`0ee5660f`
- 阶段三恢复守则注入提交（待本次实施生成）

相关记忆：
- [[at-prefix-subagent-intent]] — 阶段三@前缀方案
- [[main-subagent-no-interaction-channel]] — 主子 Agent 交互通道总览
- [[main-subagent-interaction-stage2]] — 阶段二异步路径
- [[stage2-ask-main-agent-stop-deadlock]] — 5 个死锁约束
