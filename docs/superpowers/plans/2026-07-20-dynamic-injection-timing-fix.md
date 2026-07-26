# 动态注入时机修正 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **⚠️ 项目记忆约束（重要）**：禁止使用 git worktree（[No Worktree] 项目记忆，曾导致 19 个残留分支和版本混乱）。若使用 subagent-driven-development，子 Agent 必须在主工作区直接修改，**不得**创建 worktree 隔离环境。

**Goal:** 把动态资源注入（skill + knowledge + 脑区激活 + 脑区状态图 + interaction_habits + 后台子 Agent 清单）从 `_on_turn_end`（LLM 调用后）改到**每轮 LLM 调用前**，让当前轮 LLM 立即看到与当前 messages 状态匹配的动态注入，而不是滞后一轮。

**Architecture:** 现有逻辑在 `_on_turn_end` 内调 `_inject_dynamic_resources`，注入的 system message 修改 `messages[0]` 后只在**下一轮 LLM 调用**才被读取，导致滞后一轮。本方案新增 `on_before_llm` 回调，在 `agent_runner_loop` while 循环内 LLM 调用前调用，原地修改 `messages[0]`，让本轮 LLM 立即读到。`_on_turn_end` 内的其他逻辑（脑区衰减 `decay_all`、`_refresh_user_memories`）保留原时机——只有"动态注入"这一步从 `_on_turn_end` 移到 `on_before_llm`。

**6 处 `on_turn_end` 调用点分类**（I3 修复 + 二轮 I1 修复，implementer 必读）：

`agent/generic/agent_loop.py` 有 6 处调 `on_turn_end`，分两类：
- **对话结束分支**（4 处）：注入的 system message 永远不会被 LLM 读到（已 return）
  - L759-760：`CONTEXT_OVERFLOW` 退出（LLM 返回 context_length_exceeded）
  - L912-913：`should_exit` 退出（工具执行后决定退出）
  - L964-965：纯文本回复（无 tool_calls）退出前
  - L1046-1047：无 tool_calls 退出时（与 L964 互斥分支）
- **多轮继续分支**（1 处）：注入的 system message 会被下一轮 LLM 读到
  - L1073-1074：正常多轮每轮末尾
- **MAX_TURNS 超出分支**（1 处）：while 循环外，max_turns 超出后退出前调用
  - L1077-1078：`MAX_TURNS_EXCEEDED` 退出（注释 "MAX_TURNS_EXCEEDED 退出时也要执行衰减"）

改造后这 6 处 `on_turn_end` 调用**全部保留**（不删除），因为 `_on_turn_end` 仍需做 `_refresh_user_memories` + 脑区衰减 `decay_all`——这两个操作在对话结束分支和 MAX_TURNS 超出分支都需要执行（用户记忆刷新、脑区衰减到下一轮对话）。

**脑区激活时机语义变化**（I2 修复，implementer 必读）：

现状时序（每轮）：
```
[LLM 调用] → [_on_turn_end: refresh_memories → decay_all → activate_for_query → assemble]
```
- `activate_for_query(context)` 用本轮 LLM 结束后的 messages[-3:] 激活脑区
- 激活的脑区供**下一轮 LLM** 用（滞后一轮）

改造后时序（每轮）：
```
[_on_before_llm: extract_context → inject(含 activate_for_query) → assemble] → [LLM 调用] → [_on_turn_end: refresh_memories → decay_all]
```
- `activate_for_query(context)` 用本轮 LLM 调用前的 messages[-3:] 激活脑区
- 激活的脑区供**本轮 LLM** 用（消除滞后）
- `decay_all` 仍在本轮 LLM 结束后衰减（保留原时机）

**语义变化分析**：
1. `activate_for_query` 接受 `query_context: str`（region_injector.py L56-64），内部调 `query_data(context, top_k=20)` 检索实体激活脑区。改造前 context 含本轮 assistant 消息，改造后 context 含上一轮 tool 结果或 user 消息——两者都是合法字符串，`activate_for_query` 不依赖 context 来源的特定语义
2. 脑区激活级别由 `activate_for_query` 增加、`decay_all` 减少。改造后时序：本轮前 activate（增加）→ 本轮 LLM → 本轮后 decay（减少）→ 下一轮前 activate。激活与衰减频率不变，激活级别稳定
3. 改造后脑区激活的 context 与本轮 LLM 即将处理的 context 一致（都是 messages[-3:]）——比现状"用上轮 context 激活下轮脑区"更合理
4. **首轮 activate context 差异**（M1 修复）：首轮 history=[] 时，改造前 `_extract_context_from_history([], user_input)` 返回 `user_input` 字符串；改造后 `_extract_context_from_messages([system, user])` 返回 `f"user: {user_input[:80]}..."`。两者都含 user_input 关键词，向量检索结果近似，对脑区激活累加影响可接受。如需精确一致，可让 `_extract_context_from_messages` 在首轮 special-case，但本方案不引入该特殊逻辑（YAGNI）

**首轮注入策略**（I1 修复 + C4 修复，implementer 必读）：

现状 `runner.chat()` L2285-2310 做首轮注入：
1. L2285 `_extract_context_from_history(history, user_input)` 提取 context（输入是 history + user_input）
2. L2288 `_inject_dynamic_resources(context)` 注入
3. L2292-2306 resources 处理（"拖入文件的模式信息"，如 mode=reference/move 指令）
4. L2310 `_assemble_system_message([system_message], injection, ...)` 构造 system_message
5. L2350 传 system_message 给 agent_runner_loop 作为 messages[0]

改造后 `agent_runner_loop` 首轮 while 循环调 `_on_before_llm(messages, turn=1)`：
1. `_extract_context_from_messages(messages)` 提取 context
2. `_inject_dynamic_resources(context)` 注入
3. **`_assemble_system_message(messages, injection + self._first_turn_extra_injection, ...)` 原地改 messages[0]（C4 修复：合并首轮 resources 文本）**
4. **清空 `self._first_turn_extra_injection`（防跨对话泄漏）**

**两次注入的 context 不同**：
- L2285 用 `_extract_context_from_history`，固定拼接 `user: {user_input}` 作为最后一段
- `_on_before_llm` 用 `_extract_context_from_messages(messages[-3:])`，可能不含 user_input（如果 history 末尾是 user/assistant）

**处理策略**（C4 修复核心）：

1. **删除 L2285-2288 的首轮注入**（`_extract_context_from_history` + `_inject_dynamic_resources`），让 `_on_before_llm` 首轮统一负责注入
2. **保留 L2292-2306 的 resources 处理**，但把追加的文本存入 `self._first_turn_extra_injection`（实例属性）而非 `injection` 变量
3. **`_on_before_llm` 在 `turn == 1` 时把 `self._first_turn_extra_injection` 合并到 injection**（`injection += self._first_turn_extra_injection`），然后清空实例属性
4. **L2308-2310 的 `_assemble_system_message` 保留**，但 injection 参数用 `""`（初始 system_message 只含静态段，动态段留给 `_on_before_llm` 首轮覆盖）

**为什么需要 `_first_turn_extra_injection` 实例属性**：

C4 问题：如果直接删除 L2288 的 `injection, _ = self._inject_dynamic_resources(context)` 但保留 L2306 `injection += resources_text`，然后 L2310 `_assemble_system_message([system_message], injection, ...)` 构造含 resources 的 system_message——接着 `agent_runner_loop` 首轮 `_on_before_llm` 调 `_assemble_system_message(messages, injection2, ...)` 用**不含 resources** 的 injection2 **整体替换** messages[0].content（`messages[0]` 与 `system_message` 是同一 dict 引用），resources 文本被擦掉。

**生产功能回归**：改造前 turn-1 LLM 能读到 mode=reference/move 指令（`_on_turn_end` 在 turn-1 结束后才擦）；改造后**没有任何一轮 LLM 读得到**——拖入文件的模式要求对 LLM 永久失效。

**修复**：把 resources 文本存入实例属性 `self._first_turn_extra_injection`，`_on_before_llm` 在 `turn == 1` 时合并到 injection 后清空。这样：
- chat() 构造的初始 system_message 只含静态段（injection=""），resources 文本存实例属性
- `_on_before_llm` 首轮覆盖 messages[0] 时把 resources 合并进 injection
- 后续轮次 `turn > 1` 时实例属性已清空，不重复注入

**`_refresh_user_memories` 时机**（M3 修复）：

`_refresh_user_memories` 留在 `_on_turn_end`（不提前到 `_on_before_llm`）——原因：
1. user memory 修改发生在跨对话（用户通过 `user_memory_remember` 工具主动写入），单轮对话内不会 dirty
2. `_refresh_user_memories` 检测 dirty 后从 `~/.niu/memory.json` 读最新内容，把 user memory 段写回 `self.static_system_prompt`（`<!--USER_MEMORY_START/END-->` 标记之间），并重算 `self.base_system_prompt = static + dynamic_system_prefix`（runner.py L1774-1811）
3. 若把 refresh 提前到 `_on_before_llm`，本轮**即可**读到新 memory（`_assemble_system_message` 每次调用都实时读 `self.static_system_prompt`，runner.py L792 Claude 分支 / L802 非 Claude 分支）——但 user memory 变化是跨对话级的，本轮提前读到的收益极低，却要多付每轮一次 dirty 检查 + 潜在文件 IO（读 memory.json + 正则替换 static_system_prompt）
4. 本轮 LLM 读到上一轮的 user memory 是可接受的——不像 skill/knowledge 那样每轮都可能变化，user memory 只在用户主动写入时才 dirty，延迟一轮影响可忽略

如果未来发现 user memory 实时性不足（如工具调用频繁修改 memory.json），可以再单独把 `_refresh_user_memories` 提前到 `_on_before_llm`，但本方案不动它。

**Tech Stack:** Python 3.11+，pytest，agent/generic/agent_loop.py，agent/runner.py

---

## 现状速览（implementer 必读）

**问题诊断**（用户真实日志验证）：
- 用户消息 [175] "看一下知识库里面还有没有未命名的照片" 进入 messages
- LLM 回复 [176] 调工具 → [177] tool 结果 → [178] assistant 调工具 → [179] tool 结果 → [180] assistant 调工具 → [181] tool 结果
- `_on_turn_end` 在 [181] 后调用，`messages[-3:]` = [179][180][181]，[175] 用户消息早已被挤出窗口
- 检索 query 是 `assistant: 好的老板！让我通过file-processor查询更准确的未命名照片信息：\nchat-with-file-processor(...)`——完全偏题
- 注入的 system message 修改后只在下一轮 LLM 调用才读到——**滞后一轮**

**关键文件**：
- `agent/generic/agent_loop.py:490-510` — `agent_runner_loop` 函数签名（含 `on_turn_end` 参数）
- `agent/generic/agent_loop.py:599-1078` — while 主循环
- `agent/generic/agent_loop.py:661` — `client.chat(messages, tools)` LLM 调用点
- `agent/generic/agent_loop.py:759-760/912-913/964-965/1046-1047/1073-1074/1077-1078` — 6 处 `on_turn_end` 调用点
- `agent/runner.py:804-827` — `_on_turn_end` 实现（含 `_refresh_user_memories` + 脑区衰减 + `_inject_dynamic_resources` + `_assemble_system_message`）
- `agent/runner.py:2045-2221` — `_inject_dynamic_resources` 统一入口（脑区激活 + skill 计数器 + knowledge + interaction_habits + 子 Agent 清单）
- `agent/runner.py:2350-2365` — `runner.chat()` 调 `agent_runner_loop`，传 `on_turn_end=self._on_turn_end`

**现有 `_on_turn_end` 内的 4 件事**：
1. `_refresh_user_memories(messages)` — 刷新用户记忆（dirty 检测）
2. 脑区衰减 `decay_all()` — 脑区激活级别每轮 -N
3. `_inject_dynamic_resources(context)` — 动态注入（**这个要移到 LLM 前**）
4. `_assemble_system_message(messages, injection, ...)` — 原地改 `messages[0]`

**保留 `_on_turn_end` 内**：1（刷新用户记忆）+ 2（脑区衰减）—— 它们不依赖 LLM 调用时机
**移到 `on_before_llm`**：3 + 4 —— 它们必须在 LLM 调用前完成

**context 提取保持原样**：`_extract_context_from_messages(messages[-3:])` 不动。用户明确要求"保持 messages[-3:] 原样"——即使工具调用多轮把用户原始消息挤出窗口，也是预期行为（用户原意"最近 3 条消息不管是谁"）。

---

## File Structure

**Modify**：
- `agent/generic/agent_loop.py` — 新增 `on_before_llm` 回调参数 + 在 while 循环 LLM 调用前调用
- `agent/runner.py` — 新增 `_on_before_llm` 方法（含注入逻辑）+ `_on_turn_end` 删除注入逻辑 + `chat()` 传 `on_before_llm`

**Create**：
- `tests/test_on_before_llm_callback.py` — on_before_llm 回调单元测试（Task 1）
- `tests/test_on_before_llm_method.py` — NiuRunner._on_before_llm 方法单元测试（Task 2）

**仅回归运行（不修改）**：`tests/test_dynamic_injection_per_turn.py`

---

## Task 0: 实施前置 — gitnexus impact 分析 + 临时备份

**Files:** 无（只读分析 + git 操作）

**设计**：CLAUDE.md 铁律第 3、4 条要求修改前必须先做 gitnexus impact 分析 + 临时提交备份。

- [ ] **Step 1: gitnexus impact 分析（由主对话通过 MCP 工具完成）**

主对话调用以下 MCP 工具（不是 shell 命令）：
```
gitnexus_impact({target: "agent_runner_loop", direction: "upstream"})
gitnexus_impact({target: "_on_turn_end", direction: "upstream"})
gitnexus_impact({target: "_inject_dynamic_resources", direction: "upstream"})
```

implementer 在 Task 0 阶段应：
1. 在主对话里请求"帮我跑 gitnexus impact 分析 agent_runner_loop / _on_turn_end / _inject_dynamic_resources"
2. 主对话调用上述 MCP 工具，返回 blast radius 报告
3. implementer 接收报告后向用户报告 HIGH 风险并获确认

风险等级预期：HIGH（agent_runner_loop 是所有 Agent 路径的核心循环，主 Agent / 子 Agent / 挂起恢复都走这里）。

- [ ] **Step 2: 临时备份**

```bash
cd <repo_root>
git add -A && git commit -m "backup: before dynamic injection timing refactor (baseline)"
```

如果当前工作区干净（git status 无变化），跳过本步。

- [ ] **Step 3: 报告 blast radius 给用户**

把 Step 1 输出的 impact 分析结果整理成简短报告（直接调用方、影响进程、风险等级），等用户确认后才能进入 Task 1。

---

## Task 1: 新增 `on_before_llm` 回调参数 + 在 while 循环 LLM 调用前调用

**Files:**
- Modify: `agent/generic/agent_loop.py:490-510`（函数签名加参数）
- Modify: `agent/generic/agent_loop.py:599-661`（while 循环内 LLM 调用前调用回调）
- Create: `tests/test_on_before_llm_callback.py`

**设计**：
- `agent_runner_loop` 函数签名新增 `on_before_llm` 参数（可选 callable，默认 None）
- 在 while 循环内 LLM 调用（L661 `client.chat`）之前调用 `on_before_llm(messages, turn)` 如果非 None
- 回调签名：`on_before_llm(messages: list, turn: int) -> None`（原地修改 messages[0]）

### Task 1 Step 1: 写失败测试

- [ ] **Step 1: 写失败测试**

```python
# tests/test_on_before_llm_callback.py
"""on_before_llm 回调单元测试。

验证：
1. on_before_llm 在 LLM 调用前被调用
2. 每轮 LLM 调用前都会调用（不是只首轮）
3. 不传 on_before_llm 时正常工作（向后兼容）
4. on_before_llm 抛异常时仅 warning、对话继续（异常容错）

关键 mock 要点（三轮 C1/C2/C3 修复）：
- C1：patch 目标必须是源模块 agent.runner（is_stop_requested/clear_stop/drain_supplement
  在 agent/runner.py L45/L50/L121 定义），agent_loop.py 是函数内 import（L511），
  模块命名空间不存在这些属性，patch agent.generic.agent_loop 会 AttributeError
- C2：client.chat 返回的 generator 必须 yield + return 同一个 response
  （agent_loop L317-322 exhaust 取 return value；只 yield 不 return 会拿到 None）
- C3：dispatch side_effect 必须返回 generator 实例（_make_dispatch_gen() 调用），
  不能返回 generator 函数本身（否则 next() 会 TypeError）
- tool_calls 用 Mock 而非 MagicMock（MagicMock(name=...) 的 name 是构造参数不是属性）
- 所有测试统一 patch _intercept_at_prefix_content（M2 修复，verbose=False 路径必调）
"""
from unittest.mock import MagicMock, Mock, patch
from contextlib import ExitStack
from agent.generic.agent_loop import agent_runner_loop, StepOutcome, exhaust


def _common_patches(stack: ExitStack):
    """统一的 patch 集合，所有测试都用（C1 修复：patch 源模块 agent.runner）"""
    # is_stop_requested/clear_stop/drain_supplement 在 agent.runner 定义，
    # agent_loop.py L511 函数内 import——patch 源模块才生效
    stack.enter_context(patch("agent.runner.is_stop_requested", return_value=False))
    stack.enter_context(patch("agent.runner.clear_stop"))
    stack.enter_context(patch("agent.runner.drain_supplement"))
    stack.enter_context(patch("agent.generic.agent_loop._enforce_message_budget", side_effect=lambda m: m))
    stack.enter_context(patch("agent.generic.agent_loop._fifo_prune", return_value=0))
    stack.enter_context(patch("agent.generic.agent_loop.count_messages_tokens", return_value=100))
    # M2 修复：verbose=False 路径必调 _intercept_at_prefix_content，统一 patch 返回无拦截
    stack.enter_context(patch("agent.generic.agent_loop._intercept_at_prefix_content", return_value=(False, None)))


def _make_response(content="test response", tool_calls=None):
    """构造一个 mock LLM response"""
    response = MagicMock()
    response.content = content
    response.tool_calls = tool_calls  # None 表示无 tool_calls
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response.context_overflow = False
    return response


def _make_chat_gen(response):
    """构造一个 client.chat 返回的 generator（C2 修复：yield + return 同一个 response）

    agent_loop L663 `response = yield from response_gen`（verbose=True）
    或 L666 `response = exhaust(response_gen)`（verbose=False）取 return value。
    只 yield 不 return 时 exhaust 拿 StopIteration.value=None，后续 response.content 会 AttributeError。
    """
    def _gen(*args, **kwargs):
        yield response
        return response  # 关键：exhaust 取 return value
    return _gen()


def _make_tool_call(tc_id: str, tool_name: str, args_json: str):
    """构造一个 Mock tool_call（用 Mock 而非 MagicMock，避免 name 参数歧义）"""
    tc = Mock()
    tc.id = tc_id
    tc.function = Mock()
    tc.function.name = tool_name
    tc.function.arguments = args_json
    return tc


def _make_dispatch_gen(outcome: StepOutcome):
    """构造一个 dispatch generator 工厂（C3 修复：调用 _gen() 返回 generator 实例）

    agent_loop L851 `gen = handler.dispatch(...)` + L857 `exhaust(gen)` 取 return value。
    dispatch 必须是 generator 实例，不能是 generator 函数（否则 next() 会 TypeError）。
    参考 tests/test_dynamic_injection_per_turn.py:_make_handler.mock_dispatch 写法。
    """
    def _gen(*args, **kwargs):
        yield  # 让 dispatch 成为 generator
        return outcome
    return _gen()  # 关键：调用 _gen() 返回 generator 实例，不是返回函数本身


def test_on_before_llm_called_before_first_llm_call():
    """首轮 LLM 调用前，on_before_llm 被调用一次"""
    client = MagicMock()
    client.chat.return_value = _make_chat_gen(_make_response(tool_calls=None))

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []

    call_log = []

    def on_before_llm(messages, turn):
        call_log.append(("before_llm", turn, len(messages)))

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            on_before_llm=on_before_llm,
        )
        final = exhaust(gen)  # 用 exhaust 取 return value，不用 list(gen)

    assert len(call_log) >= 1, "on_before_llm 应被调用至少一次"
    assert call_log[0] == ("before_llm", 1, 2), f"首次调用应是 turn=1, messages含system+user=2条，实际: {call_log[0]}"


def test_on_before_llm_called_every_turn():
    """多轮 LLM 调用前，on_before_llm 每轮都被调用"""
    client = MagicMock()
    # 第一轮：返回 tool_calls，让循环继续
    response1 = _make_response(
        content="调用工具",
        tool_calls=[_make_tool_call("tc1", "test_tool", '{"x": 1}')],
    )
    # 第二轮：无 tool_calls，退出
    response2 = _make_response(content="done", tool_calls=None)

    responses = [response1, response2]
    def _chat_gen(*args, **kwargs):
        resp = responses.pop(0)
        yield resp
        return resp  # C2 修复：yield + return 同一个 response
    client.chat.side_effect = [_chat_gen(), _chat_gen()]

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []
    # C2+C3 修复：dispatch side_effect 每次调用都返回新 generator 实例
    handler.dispatch = MagicMock(side_effect=lambda *a, **kw: _make_dispatch_gen(
        StepOutcome(data={"content": "tool result", "tool_use_id": "tc1"}, next_prompt="继续", should_exit=False)
    ))

    call_log = []

    def on_before_llm(messages, turn):
        call_log.append(turn)

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            on_before_llm=on_before_llm,
            on_turn_end=lambda m, t, n: t,  # no-op：原样返回 tools_schema（契约 (messages, tools_schema, turn) -> tools_schema）
        )
        final = exhaust(gen)

    assert len(call_log) == 2, f"应被调用 2 次（每轮 LLM 调用前），实际: {len(call_log)}"
    assert call_log == [1, 2], f"应按 turn 顺序调用，实际: {call_log}"


def test_on_before_llm_none_backward_compatible():
    """不传 on_before_llm 时，agent_runner_loop 正常工作（向后兼容）

    用 exhaust(gen) 取 return value 验证最终 result。
    response.tool_calls=None 走 CURRENT_TASK_DONE 分支（非 EXITED）。
    """
    client = MagicMock()
    client.chat.return_value = _make_chat_gen(_make_response(tool_calls=None))

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            # 不传 on_before_llm
        )
        final = exhaust(gen)  # 取 return value

    # response.tool_calls=None → agent_loop L977 或 L1053 return {"result": "CURRENT_TASK_DONE", ...}
    # 不是 EXITED（EXITED 在 L917 should_exit=True 路径）
    assert isinstance(final, dict), f"final 应是 dict（generator return value），实际: {type(final)}"
    assert final.get("result") == "CURRENT_TASK_DONE", \
        f"无 tool_calls 应走 CURRENT_TASK_DONE 分支，实际 result: {final.get('result')}"


def test_on_before_llm_exception_does_not_break_loop():
    """on_before_llm 抛异常时 agent_loop 继续（注入失败仅 warning，对话继续）—— M3 修复"""
    client = MagicMock()
    client.chat.return_value = _make_chat_gen(_make_response(tool_calls=None))

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []

    def on_before_llm_raises(messages, turn):
        raise RuntimeError("injection failed")

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            on_before_llm=on_before_llm_raises,
        )
        final = exhaust(gen)

    # on_before_llm 抛异常被 agent_loop try/except 捕获（logger.warning），对话继续
    # client.chat 仍被调用，最终正常返回
    assert client.chat.called, "on_before_llm 抛异常后 client.chat 仍应被调用"
    assert isinstance(final, dict), f"final 应是 dict，实际: {type(final)}"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd <repo_root> && python -m pytest tests/test_on_before_llm_callback.py -v`
Expected: FAIL with "TypeError: agent_runner_loop() got an unexpected keyword argument 'on_before_llm'"

- [ ] **Step 3: 在 `agent_runner_loop` 函数签名加参数**

Modify `agent/generic/agent_loop.py:490-510` 函数签名末尾加：

```python
    on_before_llm=None,  # Optional: callback(messages, turn) called before each LLM call; modifies messages[0] in place
```

完整签名（在 `resumed_messages=None,` 后加新参数）：
```python
def agent_runner_loop(
    client,
    system_prompt: str = "",  # 向后兼容（system_message 优先）
    user_input=None,
    handler=None,
    tools_schema=None,
    max_turns=40,
    verbose=True,
    initial_user_content=None,
    history=None,  # Optional: list of {"role": "user/assistant", "content": str}
    on_turn_end=None,  # Optional: callback(messages, tools_schema, turn) -> tools_schema
    context_window_tokens=0,  # 0 means no limit check (backward compatible)
    context_fifo_threshold=0,  # 0 means no FIFO truncation; >0 means max token budget for sub-agents
    context_target_threshold=0,  # FIFO 裁剪目标 token 量
    on_context_high_usage=None,  # 主Agent超阈值回调；None=子Agent走FIFO
    enable_supplement=True,  # False for sub-agents to prevent stealing main agent's supplements
    system_message: Optional[dict] = None,  # 已组装好的 system message（首轮即带 cache_control）
    supplement_drain=None,  # 子 Agent 传入自己的 drain 函数；None 时走全局 drain_supplement
    memory_context: Optional[Any] = None,  # 阶段二新增：异步子 Agent 进度数据，None=主 Agent 路径不更新
    resumed_messages=None,  # 阶段四新增：挂起恢复路径，传入则跳过 messages 构造直接用
    on_before_llm=None,  # Optional: callback(messages, turn) called before each LLM call; modifies messages[0] in place
):
```

- [ ] **Step 4: 在 while 循环 LLM 调用前调用 `on_before_llm`**

**实施前置**（I4 修复）：implementer 必须先 `Read agent/generic/agent_loop.py L655-665` 确认实际代码的精确缩进和注释，再用 Edit 工具做替换。下面给的"修改前"代码片段仅作参考，实际 old_string 必须以 Read 结果为准（缩进、空行、注释都可能略有差异）。

Modify `agent/generic/agent_loop.py:655-665`（在 `response_gen = client.chat(...)` 之前插入）：

**修改前**（参考，实际以 Read 结果为准）：
```python
            except Exception:
                pass  # 进度更新失败不影响主流程
        response_gen = client.chat(messages=messages, tools=tools_schema)
```

**修改后**：
```python
            except Exception:
                pass  # 进度更新失败不影响主流程
        # 动态注入：每轮 LLM 调用前刷新 system message（skill/knowledge/脑区/habits）
        # 关键：必须在 client.chat 之前，让本轮 LLM 立即读到新 system message
        if on_before_llm is not None:
            try:
                on_before_llm(messages, turn)
            except Exception as e:
                logger.warning(f"[AgentLoop] on_before_llm callback failed: {e}")
        response_gen = client.chat(messages=messages, tools=tools_schema)
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd <repo_root> && python -m pytest tests/test_on_before_llm_callback.py -v`
Expected: 4 个测试全 PASS

- [ ] **Step 6: 运行既有 agent_loop 相关测试确保无回归**

Run: `cd <repo_root> && python -m pytest tests/test_dynamic_injection_per_turn.py tests/test_lightrag_retrieval_migration.py -v 2>&1 | tail -20`
Expected: 既有测试全 PASS（向后兼容，不传 on_before_llm 时行为不变）

- [ ] **Step 7: 提交**

```bash
cd <repo_root>
git add agent/generic/agent_loop.py tests/test_on_before_llm_callback.py
git commit -m "feat(agent-loop): 新增 on_before_llm 回调（每轮 LLM 调用前触发）"
```

---

## Task 2: 在 NiuRunner 新增 `_on_before_llm` 方法 + 从 `_on_turn_end` 移除注入逻辑

**Files:**
- Modify: `agent/runner.py:804-827` — `_on_turn_end` 删除注入相关步骤
- Modify: `agent/runner.py` — 新增 `_on_before_llm` 方法
- Modify: `agent/runner.py:2350-2365` — `chat()` 调 agent_runner_loop 时传 `on_before_llm=self._on_before_llm`
- Create: `tests/test_on_before_llm_method.py`

**设计**：
- `_on_before_llm(messages, turn)`：调 `_extract_context_from_messages(messages)` → `_inject_dynamic_resources(context)` → `_assemble_system_message(messages, injection, ...)`
- `_on_turn_end` 只保留：`_refresh_user_memories(messages)` + 脑区衰减 `decay_all()`
- `chat()` 调 agent_runner_loop 时传 `on_before_llm=self._on_before_llm`（首轮注入靠它），**删除 `runner.chat()` L2285-2288 的首轮注入**（I1 修复，避免重复注入 + context 不一致）

### Task 2 Step 1: 写失败测试

- [ ] **Step 1: 写失败测试**

```python
# tests/test_on_before_llm_method.py
"""NiuRunner._on_before_llm 方法单元测试。

验证：
1. _on_before_llm 调用 _inject_dynamic_resources + _assemble_system_message
2. _on_before_llm 修改 messages[0] 的 content（注入生效）
3. _on_turn_end 不再调 _inject_dynamic_resources（注入已移走）
4. 首轮（turn=1）合并 _first_turn_extra_injection（C4：拖入文件 resources 模式要求）
5. 第二轮（turn=2）不再合并（C4：实例属性已清空）
"""
import pytest
from unittest.mock import MagicMock, patch
from agent.runner import NiuRunner


@pytest.fixture
def runner():
    """构造一个最小化 NiuRunner 实例（C2 + M1 修复：补齐 _inject_dynamic_resources 访问的所有属性）

    故意跳过 __init__，已预填 _inject_dynamic_resources 当前实际访问的所有实例属性；
    若未来 _inject_dynamic_resources 新增实例属性访问，需同步更新此 fixture。
    """
    runner = NiuRunner.__new__(NiuRunner)
    # skill 计数器相关（_inject_dynamic_resources L2154-2167 访问）
    runner._skill_score_counter = {}
    runner._skill_entity_cache = {}
    # _assemble_system_message 访问（C2 修复：缺 dynamic_system_prefix 必跑 AttributeError）
    runner.default_model = "test-model"
    runner.static_system_prompt = "STATIC SYSTEM PROMPT"
    runner.dynamic_system_prefix = ""  # C2 修复：_assemble_system_message L782 访问
    # _format_lightrag_entities_for_prompt 访问的两个黑名单（类属性，L1859-1860 定义）
    runner._INJECT_ENTITY_TYPE_BLACKLIST = set()
    runner._INJECT_ENTITY_NAME_BLACKLIST = set()
    return runner


def test_on_before_llm_calls_inject_and_assemble(runner):
    """_on_before_llm 调 _inject_dynamic_resources + _assemble_system_message"""
    runner._inject_dynamic_resources = MagicMock(return_value=("INJECTION TEXT", {}))
    runner._assemble_system_message = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")

    messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(messages, turn=1)

    runner._extract_context_from_messages.assert_called_once_with(messages)
    runner._inject_dynamic_resources.assert_called_once_with("CONTEXT")
    runner._assemble_system_message.assert_called_once()
    # _assemble_system_message 的第 2 个参数应是 injection 文本
    args = runner._assemble_system_message.call_args
    assert args[0][1] == "INJECTION TEXT" or args.kwargs.get("injection") == "INJECTION TEXT"


def test_on_before_llm_modifies_messages_zero(runner):
    """_on_before_llm 修改 messages[0] 的 content（注入生效）

    走真实 _inject_dynamic_resources + _assemble_system_message 路径。
    C2 修复：fixture 已补 dynamic_system_prefix，_assemble_system_message 可正常调用。
    M1 修复：mock 掉 _format_running_subagents_section 避免真实 SubagentRegistry 副作用。
    """
    # 不 mock _inject_dynamic_resources，走真实路径
    runner._get_brain_injector = MagicMock(return_value=None)
    # M1 修复：mock 子 Agent 清单段，避免真实 SubagentRegistry.list_running() 副作用
    runner._format_running_subagents_section = MagicMock(return_value="")

    with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as MockAdapter:
        MockAdapter.return_value.search_multi_lightrag.return_value = {"skill": [], "knowledge": [], "other": []}
        MockAdapter.return_value.search_within_region.return_value = {"skill": [], "knowledge": [], "other": []}
        MockAdapter.return_value.search_interaction_habits.return_value = []
        runner._brain_adapter = MockAdapter.return_value

        messages = [{"role": "system", "content": "old content"}, {"role": "user", "content": "hello"}]
        runner._on_before_llm(messages, turn=1)

    # messages[0] 的 content 应被修改（_assemble_system_message 内部原地改）
    # Claude 路径改成 list，其他模型改字符串，都改变 content
    assert messages[0]["content"] != "old content", "messages[0] content 应被 _assemble_system_message 修改"


def test_on_turn_end_no_longer_calls_inject(runner):
    """_on_turn_end 不再调 _inject_dynamic_resources（注入已移到 _on_before_llm）"""
    runner._inject_dynamic_resources = MagicMock(return_value=("INJECTION", {}))
    runner._assemble_system_message = MagicMock()
    runner._refresh_user_memories = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")

    # patch 脑区衰减
    with patch("agent.brain_tools.get_activation_mgr", return_value=MagicMock()):
        messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
        runner._on_turn_end(messages, tools_schema=[], turn=1)

    # _inject_dynamic_resources 不应被调用
    runner._inject_dynamic_resources.assert_not_called()
    # _assemble_system_message 也不应被调用
    runner._assemble_system_message.assert_not_called()
    # 但 _refresh_user_memories 应被调用（保留）
    runner._refresh_user_memories.assert_called_once()


def test_on_before_llm_first_turn_merges_resources(runner):
    """C4 修复：_on_before_llm 首轮合并 _first_turn_extra_injection（resources 模式要求）

    拖入文件时 chat() 把 mode=reference/move 指令存入 self._first_turn_extra_injection，
    _on_before_llm 首轮（turn=1）把它合并进 injection，让首轮 LLM 能读到。
    """
    runner._inject_dynamic_resources = MagicMock(return_value=("DYNAMIC_INJECTION", {}))
    runner._assemble_system_message = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")
    # 模拟 chat() 已存入 resources 文本
    runner._first_turn_extra_injection = "\n\n【文件操作模式要求】\n- 文件 x.pdf：必须使用引用模式（mode=reference）"

    messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(messages, turn=1)

    # _assemble_system_message 收到的 injection 应含 resources 文本
    args = runner._assemble_system_message.call_args
    injection_arg = args[0][1]
    assert "DYNAMIC_INJECTION" in injection_arg, "应含动态注入文本"
    assert "文件操作模式要求" in injection_arg, "应含 resources 模式要求文本"
    assert "mode=reference" in injection_arg, "应含具体 mode 指令"
    # 实例属性应被清空（防跨对话泄漏）
    assert runner._first_turn_extra_injection == "", "首轮合并后应清空"


def test_on_before_llm_second_turn_no_resources_merge(runner):
    """C4 修复：_on_before_llm 第二轮（turn=2）不再合并 resources（已清空）"""
    runner._inject_dynamic_resources = MagicMock(return_value=("DYNAMIC_INJECTION", {}))
    runner._assemble_system_message = MagicMock()
    runner._extract_context_from_messages = MagicMock(return_value="CONTEXT")
    # 模拟首轮已清空（首轮合并后状态）
    runner._first_turn_extra_injection = ""

    messages = [{"role": "system", "content": "old"}, {"role": "user", "content": "hi"}]
    runner._on_before_llm(messages, turn=2)

    # 第二轮不合并 resources（实例属性已空）
    args = runner._assemble_system_message.call_args
    injection_arg = args[0][1]
    assert injection_arg == "DYNAMIC_INJECTION", "第二轮应只含动态注入，不含 resources"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd <repo_root> && python -m pytest tests/test_on_before_llm_method.py -v`
Expected: FAIL（`NiuRunner` 没有 `_on_before_llm` 方法）

- [ ] **Step 3: 临时备份**

```bash
cd <repo_root>
git add -A && git commit -m "backup: before _on_before_llm method integration"
```

如果工作区干净则跳过。

- [ ] **Step 4: 新增 `_on_before_llm` 方法**

Modify `agent/runner.py:804-827` `_on_turn_end` 方法之前或之后，新增 `_on_before_llm`：

```python
    def _on_before_llm(self, messages: list, turn: int) -> None:
        """每轮 LLM 调用前刷新动态注入（skill/knowledge/脑区/habits）。

        关键：在 client.chat 之前调，让本轮 LLM 立即读到新 system message。
        原地修改 messages[0]，无返回值。

        Args:
            messages: agent_runner_loop 的消息列表引用
            turn: 当前轮次（从 1 开始）
        """
        # 提取最近 3 条消息作为 context（保持原样，按用户原始设计）
        context = self._extract_context_from_messages(messages)
        injection, _ = self._inject_dynamic_resources(context)

        # C4 修复：首轮合并拖入文件的 resources 模式要求（chat() 存入实例属性）
        # chat() L2292-2306 把 resources 模式文本存入 self._first_turn_extra_injection，
        # 这里 turn==1 时合并进 injection，让首轮 LLM 能读到 mode=reference/move 指令
        if turn == 1 and getattr(self, "_first_turn_extra_injection", ""):
            injection += self._first_turn_extra_injection
            self._first_turn_extra_injection = ""  # 清空，防跨对话泄漏

        # 原地修改 messages[0]，本轮 LLM 立即读到
        self._assemble_system_message(messages, injection, self.default_model)
```

同时在 `agent/runner.py` 的 `NiuRunner.__init__` 里初始化 `self._first_turn_extra_injection = ""`（其他 `self._xxx = ""` 附近），并在 `chat()` 方法开头重置（防跨对话泄漏）：

```python
# 在 __init__ 里（其他 self._xxx = "" 附近）
self._first_turn_extra_injection: str = ""

# 在 chat() 方法开头（L2283 附近，self._current_channel_id = channel_id 之后）
self._first_turn_extra_injection = ""  # 重置首轮 resources 注入，防跨对话泄漏
```

- [ ] **Step 5: 从 `_on_turn_end` 删除注入相关步骤**

Modify `agent/runner.py:804-827` `_on_turn_end` 方法：

**修改前**：
```python
    def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
        """每轮循环结束后刷新动态注入（skills/knowledge only, no MCP schema refresh)."""
        # Refresh user memories if dirty
        self._refresh_user_memories(messages)

        # Decay brain region activation levels
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                mgr.decay_all()
        except Exception as e:
            logger.debug(f"Brain region decay failed: {e}")

        # Extract context and re-inject skills/knowledge
        context = self._extract_context_from_messages(messages)
        injection, _ = self._inject_dynamic_resources(context)

        # Update system_prompt（静态段 + 动态段，Claude 走 cache_control）
        # messages 是 agent_loop 内部列表的引用，原地修改生效
        self._assemble_system_message(messages, injection, self.default_model)

        # No schema refresh — tools_schema stays base + disk
        return tools_schema
```

**修改后**：
```python
    def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
        """每轮循环结束后的清理工作（动态注入已移到 _on_before_llm）。

        保留：
        - _refresh_user_memories：刷新用户长期记忆（dirty 检测）
        - 脑区衰减 decay_all：每轮降低脑区激活级别

        已移除（移到 _on_before_llm）：
        - _inject_dynamic_resources + _assemble_system_message
          原因：原在 LLM 调用后注入，注入的 system message 下一轮才被读到，滞后一轮
        """
        # Refresh user memories if dirty
        self._refresh_user_memories(messages)

        # Decay brain region activation levels
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                mgr.decay_all()
        except Exception as e:
            logger.debug(f"Brain region decay failed: {e}")

        # No schema refresh — tools_schema stays base + disk
        return tools_schema
```

- [ ] **Step 6: 在 `chat()` 调 `agent_runner_loop` 时传 `on_before_llm`**

Modify `agent/runner.py:2350-2365` `gen = agent_runner_loop(...)` 调用：

**修改前**：
```python
        gen = agent_runner_loop(
            client=self.client,
            system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
            system_message=system_message,
            user_input=user_input,
            handler=self.handler,
            tools_schema=tools_schema,
            max_turns=max_turns,
            verbose=False,
            initial_user_content=user_input,
            history=history,  # Pass history to agent_loop
            on_turn_end=self._on_turn_end,  # 每轮结束后刷新动态注入
            context_window_tokens=context_window_tokens,  # 主 Agent 溢出检测
            on_context_high_usage=self._on_context_high_usage,  # 主 Agent 超阈值回调
            context_target_threshold=0,  # 主 Agent 不需要 FIFO 目标阈值
            ...
        )
```

**修改后**（在 `on_turn_end` 行后加 `on_before_llm`）：
```python
        gen = agent_runner_loop(
            client=self.client,
            system_prompt="",  # 向后兼容（system_message 非 None 时分支选择生效）
            system_message=system_message,
            user_input=user_input,
            handler=self.handler,
            tools_schema=tools_schema,
            max_turns=max_turns,
            verbose=False,
            initial_user_content=user_input,
            history=history,  # Pass history to agent_loop
            on_turn_end=self._on_turn_end,  # 每轮结束后清理（用户记忆 + 脑区衰减）
            on_before_llm=self._on_before_llm,  # 每轮 LLM 调用前刷新动态注入
            context_window_tokens=context_window_tokens,  # 主 Agent 溢出检测
            on_context_high_usage=self._on_context_high_usage,  # 主 Agent 超阈值回调
            context_target_threshold=0,  # 主 Agent 不需要 FIFO 目标阈值
            ...
        )
```

注意（I1 修复 + C4 修复）：**删除 `runner.chat()` L2285-2288 的首轮注入**（`_extract_context_from_history` + `_inject_dynamic_resources`），让 `_on_before_llm` 首轮统一负责注入。但保留 L2292-2306 的 resources 处理（"拖入文件的模式信息"），**把追加的文本存入 `self._first_turn_extra_injection`（实例属性）而非 `injection` 变量**——让 `_on_before_llm` 首轮合并进 injection（C4 修复：否则 resources 文本被 `_assemble_system_message` 整体替换覆盖）。

具体改动：

读 `agent/runner.py:2283-2310` 找到首轮注入段：

**修改前**（实际代码，L2283-2310）：
```python
        self._current_channel_id = channel_id
        # 从消息历史中提取上下文
        context = self._extract_context_from_history(history, user_input)

        # 动态注入资源（skills/knowledge only）
        injection, _ = self._inject_dynamic_resources(context)

        # 注入 resources（拖入文件的模式信息）到动态段
        # （首轮特有，后续轮次 _on_turn_end 不会重新加，符合预期）
        if resources:
            # 防御性过滤：只处理格式正确的资源条目
            valid_resources = [r for r in resources if isinstance(r, dict) and "path" in r and "mode" in r]
            if valid_resources:
                resource_lines = []
                for r in valid_resources:
                    path = r.get("path", "")
                    mode = r.get("mode", "copy")
                    if mode == "reference":
                        resource_lines.append(f"- 文件 {path}：必须使用引用模式（mode=reference），不要拷贝文件，使用原路径引用")
                    elif mode == "move":
                        resource_lines.append(f"- 文件 {path}：必须使用移动模式（mode=move），将文件移动到存储目录")
                    # mode="copy" 不需要额外提示，这是默认行为
                if resource_lines:
                    injection += "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n" + "\n".join(resource_lines)

        # 组装 system message（首轮就按 model 决定格式，Claude 走 cache_control）
        system_message = {"role": "system", "content": ""}
        self._assemble_system_message([system_message], injection, self.default_model)
```

**修改后**（I1 修复 + C4 修复：resources 存实例属性，_on_before_llm 首轮合并）：
```python
        self._current_channel_id = channel_id
        # 重置首轮 resources 注入，防跨对话泄漏
        self._first_turn_extra_injection = ""

        # I1 修复：首轮动态注入由 _on_before_llm 统一负责（在 agent_runner_loop 内 turn=1 时调）
        # 这里不调用 _inject_dynamic_resources，动态注入段留给 _on_before_llm 首轮覆盖

        # 注入 resources（拖入文件的模式信息）到实例属性
        # C4 修复：存 self._first_turn_extra_injection 而非 injection 变量，
        # 让 _on_before_llm 首轮合并进 injection（否则被 _assemble_system_message 整体替换覆盖）
        if resources:
            # 防御性过滤：只处理格式正确的资源条目
            valid_resources = [r for r in resources if isinstance(r, dict) and "path" in r and "mode" in r]
            if valid_resources:
                resource_lines = []
                for r in valid_resources:
                    path = r.get("path", "")
                    mode = r.get("mode", "copy")
                    if mode == "reference":
                        resource_lines.append(f"- 文件 {path}：必须使用引用模式（mode=reference），不要拷贝文件，使用原路径引用")
                    elif mode == "move":
                        resource_lines.append(f"- 文件 {path}：必须使用移动模式（mode=move），将文件移动到存储目录")
                    # mode="copy" 不需要额外提示，这是默认行为
                if resource_lines:
                    self._first_turn_extra_injection = "\n\n【文件操作模式要求】\n以下文件的操作模式由用户指定，调用 ingest 工具时必须传递对应的 mode 参数：\n" + "\n".join(resource_lines)

        # 组装 system message（首轮就按 model 决定格式，Claude 走 cache_control）
        # injection="" 因为动态注入移到 _on_before_llm 首轮
        # resources 文本在实例属性里，_on_before_llm 首轮会合并进 injection
        system_message = {"role": "system", "content": ""}
        self._assemble_system_message([system_message], "", self.default_model)
```

**关键改动**：
1. 删除 L2285-2288 的 `context = self._extract_context_from_history(...)` + `injection, _ = self._inject_dynamic_resources(context)`
2. **L2292-2306 的 resources 处理保留，但把 `injection += ...` 改为 `self._first_turn_extra_injection = ...`**（C4 修复核心：存实例属性而非局部变量）
3. **L2308-2310 的 `_assemble_system_message` 调用保留，injection 参数改为 `""`**（初始 system_message 只含静态段，动态段留给 `_on_before_llm` 首轮覆盖）
4. 在 `chat()` 开头加 `self._first_turn_extra_injection = ""` 重置（防跨对话泄漏）

**为什么这样改**：
- 如果保留 `injection += resources_text` + `_assemble_system_message([system_message], injection, ...)` 构造含 resources 的 system_message，然后 `_on_before_llm` 首轮用**不含 resources** 的 injection2 整体替换 messages[0].content——resources 文本被擦掉（C4 问题）
- 改为存实例属性后，`_on_before_llm` 首轮在注入时把 `self._first_turn_extra_injection` 合并进 injection，messages[0].content 含完整 resources + 动态注入

**注意**：`_extract_context_from_history` 方法本身不删除（其他地方可能还用），只删 `chat()` 内的调用。如果确认无其他调用方，可后续清理——本方案不强制清理。

- [ ] **Step 7: 运行测试验证通过**

Run: `cd <repo_root> && python -m pytest tests/test_on_before_llm_method.py tests/test_on_before_llm_callback.py tests/test_skill_inject_integration.py tests/test_skill_score_counter.py -v`
Expected: 全 PASS

- [ ] **Step 8: 运行现有相关测试确保无回归**

Run: `cd <repo_root> && python -m pytest tests/test_dynamic_injection_per_turn.py tests/test_lightrag_retrieval_migration.py tests/test_lightrag_manager.py -v 2>&1 | tail -20`
Expected: 现有测试全 PASS（如有失败需分析是否本方案引入的回归）

- [ ] **Step 9: 提交**

```bash
cd <repo_root>
git add agent/runner.py tests/test_on_before_llm_method.py
git commit -m "refactor(inject): 动态注入从 _on_turn_end 移到 _on_before_llm（每轮 LLM 调用前）"
```

---

## Task 3: 真实程序验证

**Files:** 无（只跑真实程序）

**设计**：用真实程序 + 真实 LLM 验证：每轮 LLM 调用前注入，注入的 system message 立即被当前轮 LLM 读到（不滞后一轮）。

- [ ] **Step 1: 启动程序**

```bash
cd <repo_root>
./niu &
```

- [ ] **Step 2: 触发需要工具调用的对话**

说一句会触发工具调用多轮的话（如"看一下知识库里面还有没有未命名的照片"——预期会调 lightrag_query / file-processor 多轮）。

- [ ] **Step 3: 检查日志验证注入时机**

```bash
grep -E "Dynamic injection|on_before_llm|Skill injection" logs/api_stderr.log 2>/dev/null | tail -30
```

Expected:
- 每轮 LLM 调用前都有 `Dynamic injection | ...` 日志
- 第一轮 LLM 调用前应有注入日志（用 turn=1 标识）
- 检索 query 应包含当前 messages 状态（可能含工具结果或被挤出的用户消息）

- [ ] **Step 4: 临时日志验证注入时机（可选）**

如果需要精确验证注入时机，在 `_on_before_llm` 末尾**临时**加：
```python
logger.debug(f"[BeforeLLM] turn={turn}, messages_count={len(messages)}, last_msg_role={messages[-1].get('role') if messages else 'N/A'}")
```

跑完对话后从 `logs/api_stderr.log` 读日志，确认每轮 LLM 调用前都有 `[BeforeLLM]` 日志。

- [ ] **Step 5: 关闭程序**

```bash
kill -TERM $(pgrep -f "niu")
```

- [ ] **Step 6: 撤销临时日志**

```bash
cd <repo_root>
git diff HEAD agent/runner.py tests/  # 确认无临时日志残留
grep -rn "BeforeLLM" agent/ tests/  # 兜底检查临时日志字符串
```

如果有 grep 结果，必须删除临时日志后才能进入 Step 7。

- [ ] **Step 7: 提交验证记录（无代码改动则不 commit）**

如果 Step 4 加了临时日志且已撤销，跳过本步。如果有其他小修，commit：
```bash
git add -A
git commit -m "test(inject): 真实程序验证动态注入时机（每轮 LLM 调用前）"
```

---

## Self-Review

**Spec coverage 检查**：
- ✅ 把动态注入从 `_on_turn_end` 移到每轮 LLM 调用前 → Task 1（agent_loop 加 on_before_llm 回调）+ Task 2（NiuRunner 加 _on_before_llm 方法 + 从 _on_turn_end 删除注入）
- ✅ 所有动态注入一起改（skill + knowledge + 脑区激活 + 脑区状态图 + interaction_habits + 子 Agent 清单）→ 它们都在 `_inject_dynamic_resources` 里，移动 `_inject_dynamic_resources` 调用点一起改
- ✅ context 提取保持原样 `messages[-3:]` → 不动 `_extract_context_from_messages`，按用户明确要求
- ✅ 向后兼容 → `on_before_llm=None` 默认值，不传时 agent_runner_loop 行为不变（Task 1 Step 1 第 3 个测试）
- ✅ gitnexus impact 分析 + 临时备份 → Task 0 前置
- ✅ 真实数据测试 → Task 3 用真实程序+真实 LLM 验证
- ✅ 6 处 on_turn_end 调用点分类说明（I3 修复 + 二轮 I1 修复）→ Architecture 段明确"对话结束分支（4 处）+ 多轮继续分支（1 处）+ MAX_TURNS 超出分支（1 处）"全部保留，`_on_turn_end` 仍做 refresh_memories + decay_all
- ✅ 脑区激活时机语义变化论证（I2 修复）→ Architecture 段分析 activate_for_query 接受任意字符串 context、激活/衰减频率不变、改造后 context 与本轮 LLM 即将处理的一致
- ✅ 首轮注入策略（I1 修复 + C4 修复）→ 删除 runner.chat L2285-2288 首轮注入，由 _on_before_llm 首轮统一负责；resources 模式要求存 `self._first_turn_extra_injection` 实例属性，`_on_before_llm` 首轮合并进 injection（C4 修复：否则被 `_assemble_system_message` 整体替换覆盖，导致拖入文件模式要求对 LLM 永久失效）
- ✅ _refresh_user_memories 时机说明（M3 修复）→ 留在 _on_turn_end，user memory 跨对话级变化频率低
- ✅ Edit old_string 精确性提醒（I4 修复）→ Task 1 Step 4 + Task 2 Step 6 都要求 implementer 先 Read 实际代码再 Edit
- ✅ 测试 mock 完整性（C1 + C2 + M1 + M2 + 三轮 C1/C2/C3 修复）→ Task 1 用 Mock 而非 MagicMock、patch 源模块 agent.runner（C1）、chat generator yield + return（C2）、dispatch 返回 generator 实例（C3）、_common_patches 统一 patch _intercept_at_prefix_content；Task 2 fixture 补 dynamic_system_prefix + mock _format_running_subagents_section

**Placeholder 扫描**：无 TBD/TODO/handle edge cases 等占位符。所有步骤含完整代码。

**Type consistency**：
- `on_before_llm` 回调签名 `(messages: list, turn: int) -> None` — Task 1 定义 + Task 2 `_on_before_llm` 实现签名一致
- `on_turn_end` 回调签名不变 `(messages, tools_schema, turn) -> tools_schema` — Task 2 保留
- `_on_before_llm` 与 `_on_turn_end` 都是 NiuRunner 实例方法
- Task 1 测试 mock `client.chat` 返回 generator，与 agent_loop 真实路径一致

**风险点**：
- agent_runner_loop 是核心循环，所有 Agent 路径（主 Agent / 子 Agent / 挂起恢复）都走这里——blast radius HIGH
- 子 Agent 也调 agent_runner_loop（agent/subagent.py），不传 on_before_llm 时行为不变（向后兼容），但子 Agent 也想要动态注入的话需单独传——本方案只改主 Agent，子 Agent 改动留作后续
- 首轮注入策略变更（I1 修复）：删除 `runner.chat()` L2285-2288 的首轮注入，由 `_on_before_llm` 首轮统一负责。L2310 的 system_message 构造保留（用空 injection 初始化，保证 messages[0] 非空），`_on_before_llm` 首轮调用时覆盖 messages[0] 的 dynamic 段

**Test count**：
- Task 1：4 个单元测试（on_before_llm 回调时机 + 异常容错）
- Task 2：5 个单元测试（_on_before_llm 方法 + _on_turn_end 不再注入 + C4 首轮 resources 合并 + C4 第二轮不合并）
- Task 3：真实程序验证（非自动化测试）

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-20-dynamic-injection-timing-fix.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
