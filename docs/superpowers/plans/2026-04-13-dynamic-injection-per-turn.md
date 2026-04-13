# 动态注入轮次级刷新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将动态注入从"对话级"（每次 chat() 调用一次）升级为"轮次级"（每个 turn 刷新一次），使 Skills、MCP 工具 Schema、知识等资源能在多轮循环中动态更新。

**Architecture:** 在 `agent_runner_loop()` 的每轮循环末尾，通过回调函数重新执行动态注入，更新 `messages[0]`（system_prompt）和 `tools_schema`。回调由 `runner.py` 提供，封装了 `_inject_dynamic_resources()` 和工具 Schema 组装逻辑。

**Tech Stack:** Python, agent_runner_loop generator, callback pattern

---

## 问题分析

### 根因

`agent_runner_loop()` 是多轮循环，但 `system_prompt` 和 `tools_schema` 在循环入口设置后不再更新。动态注入被设计为"对话级"而非"轮次级"。

### 影响链

```
工具在 turn N 被命中 → hit_tool() 记录到 tool_lifecycle + 产生 Pending Skills
  ↓
turn N+1 的 LLM 请求仍使用旧的 system_prompt 和 tools_schema
  ↓
Pending Skills 无法消费，新工具 Schema 无法注入，知识不会重新检索
```

### 解决方案：轮次级刷新回调

在 `agent_runner_loop()` 每轮循环末尾，调用 `on_turn_end()` 回调，由 `runner.py` 提供。回调负责：
1. 从 `messages` 中提取最新上下文
2. 重新执行 `_inject_dynamic_resources()` 更新 system_prompt
3. 重新组装 tools_schema（加入新发现的工具）
4. 更新 `messages[0]` 和 `tools_schema`

---

## 文件结构

```
agent/
├── generic/
│   └── agent_loop.py          # 添加 on_turn_end 回调参数和调用
└── runner.py                  # 提供 _on_turn_end 回调实现

tests/
└── test_dynamic_injection_per_turn.py  # 测试轮次级刷新
```

---

## 任务 1：编写轮次级刷新的失败测试

**文件：**
- 创建：`tests/test_dynamic_injection_per_turn.py`

- [ ] **步骤 1：编写失败测试**

```python
"""测试动态注入在每轮循环中刷新。"""
import pytest
import json
from unittest.mock import Mock, MagicMock


def test_system_prompt_updated_between_turns():
    """每轮循环后，system_prompt 应该通过 on_turn_end 回调更新。"""
    from agent.generic.agent_loop import agent_runner_loop

    # 追踪每轮的 system_prompt
    system_prompts_seen = []

    client = Mock()
    client.last_tools = ""

    # 模拟 LLM：第一轮调用工具，第二轮直接回复
    call_count = [0]
    mock_tool_call = Mock(
        id="call_1",
        function=Mock(name="browser-server/browser_navigate", arguments='{"url": "https://example.com"}')
    )

    def mock_chat(**kwargs):
        call_count[0] += 1
        msgs = kwargs.get("messages", [])
        # 记录每轮的 system_prompt
        if msgs and msgs[0].get("role") == "system":
            system_prompts_seen.append(msgs[0]["content"])

        if call_count[0] == 1:
            # 第一轮：调用工具
            resp = Mock()
            resp.content = ""
            resp.tool_calls = [mock_tool_call]
            return resp
        else:
            # 第二轮：直接回复
            resp = Mock()
            resp.content = "已完成"
            resp.tool_calls = None
            return resp

    client.chat = mock_chat

    # Handler：返回成功结果
    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 3

    def mock_dispatch(tool_name, args, response, index=0):
        from agent.generic.agent_loop import StepOutcome
        yield ""
        return StepOutcome(
            {"status": "success", "message": "Navigated"},
            next_prompt="工具调用成功。请向用户简洁汇报结果：Navigated",
            should_exit=False
        )

    handler.dispatch = mock_dispatch

    # on_turn_end 回调：模拟注入 Skills
    injection_count = [0]

    def on_turn_end(messages, tools_schema, turn):
        """每轮结束后，注入新的 Skills 到 system_prompt。"""
        injection_count[0] += 1
        if injection_count[0] >= 1:
            # 在第一轮后注入 browser-automation skill
            if messages and messages[0].get("role") == "system":
                current = messages[0]["content"]
                if "browser-automation" not in current:
                    messages[0]["content"] = current + "\n\n### [相关技能]\n1. **browser-automation** (分数: 100)\n   Browser automation skill\n   文件路径: memory/skills/browser-automation.md"
        return tools_schema  # 可以返回更新后的 tools_schema

    gen = agent_runner_loop(
        client=client,
        system_prompt="基础提示词",
        user_input="打开浏览器",
        handler=handler,
        tools_schema=[],
        max_turns=2,
        verbose=False,
        on_turn_end=on_turn_end,
    )

    try:
        list(gen)
    except StopIteration:
        pass

    # 验证：第二轮的 system_prompt 应该包含注入的 Skills
    assert len(system_prompts_seen) >= 2, f"应该看到至少2轮的 system_prompt，实际: {len(system_prompts_seen)}"
    assert "browser-automation" in system_prompts_seen[1], \
        f"第二轮的 system_prompt 应该包含注入的 browser-automation skill，实际: {system_prompts_seen[1][:200]}"


def test_tools_schema_updated_between_turns():
    """每轮循环后，tools_schema 应该通过 on_turn_end 回调更新。"""
    from agent.generic.agent_loop import agent_runner_loop

    client = Mock()
    client.last_tools = ""

    # 追踪每轮的 tools_schema
    tools_seen = []

    call_count = [0]
    mock_tool_call = Mock(
        id="call_1",
        function=Mock(name="browser-server/browser_navigate", arguments='{}')
    )

    def mock_chat(**kwargs):
        call_count[0] += 1
        tools_seen.append(list(kwargs.get("tools", [])))

        if call_count[0] == 1:
            resp = Mock()
            resp.content = ""
            resp.tool_calls = [mock_tool_call]
            return resp
        else:
            resp = Mock()
            resp.content = "完成"
            resp.tool_calls = None
            return resp

    client.chat = mock_chat

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 3

    def mock_dispatch(tool_name, args, response, index=0):
        from agent.generic.agent_loop import StepOutcome
        yield ""
        return StepOutcome(
            {"status": "success"},
            next_prompt="工具调用成功",
            should_exit=False
        )

    handler.dispatch = mock_dispatch

    # on_turn_end：第一轮后添加新工具 schema
    def on_turn_end(messages, tools_schema, turn):
        if turn == 1:
            new_schema = {
                "type": "function",
                "function": {
                    "name": "browser-server/browser_click",
                    "description": "Click element",
                    "parameters": {"type": "object", "properties": {}}
                }
            }
            tools_schema.append(new_schema)
        return tools_schema

    gen = agent_runner_loop(
        client=client,
        system_prompt="测试",
        user_input="测试",
        handler=handler,
        tools_schema=[],
        max_turns=2,
        verbose=False,
        on_turn_end=on_turn_end,
    )

    try:
        list(gen)
    except StopIteration:
        pass

    # 验证：第二轮应该有新工具
    assert len(tools_seen) >= 2
    assert len(tools_seen[1]) > len(tools_seen[0]), \
        f"第二轮的 tools_schema 应该比第一轮多，第一轮: {len(tools_seen[0])}, 第二轮: {len(tools_seen[1])}"


def test_on_turn_end_not_required():
    """不提供 on_turn_end 时，行为与之前完全一致（向后兼容）。"""
    from agent.generic.agent_loop import agent_runner_loop

    client = Mock()
    client.last_tools = ""

    call_count = [0]

    def mock_chat(**kwargs):
        call_count[0] += 1
        resp = Mock()
        resp.content = "完成"
        resp.tool_calls = None
        return resp

    client.chat = mock_chat

    handler = Mock()
    handler._done_hooks = []
    handler.max_turns = 1

    def mock_dispatch(tool_name, args, response, index=0):
        from agent.generic.agent_loop import StepOutcome
        yield ""
        return StepOutcome({"status": "ok"}, next_prompt="done", should_exit=False)

    handler.dispatch = mock_dispatch

    # 不提供 on_turn_end，应该正常工作
    gen = agent_runner_loop(
        client=client,
        system_prompt="测试",
        user_input="测试",
        handler=handler,
        tools_schema=[],
        max_turns=1,
        verbose=False,
    )

    try:
        list(gen)
    except StopIteration:
        pass

    assert call_count[0] == 1, "应该正常执行一轮"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pytest tests/test_dynamic_injection_per_turn.py -v`

预期：失败（`on_turn_end` 参数不存在）

---

## 任务 2：修改 agent_loop.py 支持轮次级刷新回调

**文件：**
- 修改：`agent/generic/agent_loop.py`

- [ ] **步骤 3：添加 on_turn_end 参数**

在 `agent_runner_loop()` 函数签名中添加 `on_turn_end=None` 参数：

```python
# 文件：agent/generic/agent_loop.py
# 行号：70-79

def agent_runner_loop(
    client,
    system_prompt,
    user_input,
    handler,
    tools_schema,
    max_turns=40,
    verbose=True,
    initial_user_content=None,
    history=None,  # Optional: list of {"role": "user/assistant", "content": str}
    on_turn_end=None,  # Optional: callback(messages, tools_schema, turn) -> tools_schema
):
```

- [ ] **步骤 4：在每轮循环末尾调用 on_turn_end 回调**

在 `agent_runner_loop()` 的 while 循环末尾（`next_prompt` 处理之后、下一次循环之前），添加回调调用：

```python
# 文件：agent/generic/agent_loop.py
# 在 while 循环末尾，next_prompt 处理之后添加

        # 添加下一个user消息
        messages.append({"role": "user", "content": next_prompt})

        # 轮次级刷新：调用 on_turn_end 回调更新 system_prompt 和 tools_schema
        if on_turn_end is not None:
            tools_schema = on_turn_end(messages, tools_schema, turn)
    return {"result": "MAX_TURNS_EXCEEDED"}
```

- [ ] **步骤 5：运行测试验证通过**

运行：`pytest tests/test_dynamic_injection_per_turn.py -v`

预期：3个测试全部通过

- [ ] **步骤 6：运行现有测试确保向后兼容**

运行：`pytest tests/test_agent_loop_tool_results.py tests/test_integration_tool_flow.py -v`

预期：全部通过（不提供 on_turn_end 时行为不变）

- [ ] **步骤 7：提交**

```bash
git add agent/generic/agent_loop.py tests/test_dynamic_injection_per_turn.py
git commit -m "feat: 添加轮次级动态注入回调 on_turn_end

- agent_runner_loop 支持每轮结束后刷新 system_prompt 和 tools_schema
- 通过 on_turn_end(messages, tools_schema, turn) 回调实现
- 不提供回调时行为完全不变（向后兼容）
- 解决 Pending Skills 和新工具 Schema 无法在后续轮次注入的问题

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## 任务 3：在 runner.py 实现 on_turn_end 回调

**文件：**
- 修改：`agent/runner.py`

- [ ] **步骤 8：实现 _on_turn_end 方法**

在 `NiuRunner` 类中添加 `_on_turn_end()` 方法，作为 `agent_runner_loop()` 的回调：

```python
# 文件：agent/runner.py
# 添加到 NiuRunner 类中

def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
    """
    每轮循环结束后刷新动态注入。

    Args:
        messages: 当前消息列表（可修改 messages[0] 更新 system_prompt）
        tools_schema: 当前工具 Schema 列表（可修改/返回新列表）
        turn: 当前轮次

    Returns:
        更新后的 tools_schema
    """
    # 1. 从 messages 中提取最新上下文
    context = self._extract_context_from_messages(messages)

    # 2. 重新执行动态注入
    injection = self._inject_dynamic_resources(context)

    # 3. 更新 system_prompt（messages[0]）
    if injection and messages and messages[0].get("role") == "system":
        messages[0]["content"] = self.base_system_prompt + injection

    # 4. 重新组装 tools_schema（加入新发现的工具）
    # 保留基础工具 + 基础MCP工具
    new_schema = self.base_tools_schema.copy()
    for tool_name in BASE_MCP_TOOLS:
        schema = self._get_tool_schema_by_name(tool_name)
        if schema:
            new_schema.append(schema)

    # 加入活跃工具（包括本轮新命中的）
    active_tool_names = self.tool_lifecycle.get_active_tools()
    for tool_name in active_tool_names:
        if tool_name in BASE_MCP_TOOLS:
            continue
        schema = self._get_tool_schema_by_name(tool_name)
        if schema:
            new_schema.append(schema)

    # 5. 工具衰减（每轮衰减一次）
    self.tool_lifecycle.decay_tools()

    return new_schema
```

- [ ] **步骤 9：实现 _extract_context_from_messages 方法**

从 `messages` 列表提取上下文，替代只依赖 `history` 参数的旧方法：

```python
# 文件：agent/runner.py
# 添加到 NiuRunner 类中

def _extract_context_from_messages(self, messages: list) -> str:
    """
    从 agent_runner_loop 的 messages 列表提取上下文。

    包含用户输入、LLM 回复摘要、工具调用结果，比 _extract_context_from_history
    更全面，因为它能看到循环内的实时交互。

    Args:
        messages: agent_runner_loop 的消息列表

    Returns:
        提取的上下文字符串
    """
    context_parts = []

    # 取最近的消息（最多10条）
    recent = messages[-10:] if len(messages) > 10 else messages

    for msg in recent:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "user" and content:
            # 用户消息，截断
            context_parts.append(content[:300])
        elif role == "tool" and content:
            # 工具结果，截断（工具结果可能很长）
            context_parts.append(content[:200])
        elif role == "assistant":
            # assistant 消息，取 content 部分
            if content:
                context_parts.append(content[:200])

    return " ".join(context_parts) if context_parts else ""
```

- [ ] **步骤 10：在 chat() 中传递回调给 agent_runner_loop**

修改 `chat()` 方法中调用 `agent_runner_loop()` 的部分，传入 `on_turn_end` 回调：

```python
# 文件：agent/runner.py
# 行号：523-533

# 修改前：
gen = agent_runner_loop(
    client=self.client,
    system_prompt=system_prompt,
    user_input=user_input,
    handler=self.handler,
    tools_schema=tools_schema,
    max_turns=max_turns,
    verbose=False,
    initial_user_content=user_input,
    history=history,
)

# 修改后：
gen = agent_runner_loop(
    client=self.client,
    system_prompt=system_prompt,
    user_input=user_input,
    handler=self.handler,
    tools_schema=tools_schema,
    max_turns=max_turns,
    verbose=False,
    initial_user_content=user_input,
    history=history,
    on_turn_end=self._on_turn_end,
)
```

- [ ] **步骤 11：移除 chat() 末尾的 decay_tools() 调用**

因为 `decay_tools()` 现在在 `_on_turn_end()` 中每轮调用，不再需要在 `chat()` 末尾单独调用：

```python
# 文件：agent/runner.py
# 找到并移除 chat() 末尾的 self.tool_lifecycle.decay_tools()
# 该调用现在在 _on_turn_end() 中每轮执行
```

- [ ] **步骤 12：运行所有测试**

运行：`pytest tests/ -v -k "agent_loop or tool_registry or integration or dynamic_injection"`

预期：全部通过

- [ ] **步骤 13：提交**

```bash
git add agent/runner.py
git commit -m "feat: 实现 on_turn_end 回调，轮次级刷新动态注入

- 添加 _on_turn_end() 方法，每轮结束后刷新 system_prompt 和 tools_schema
- 添加 _extract_context_from_messages() 从循环内消息提取上下文
- Pending Skills 现在能在工具命中后的下一轮被注入
- 新发现的工具 Schema 现在能在下一轮被 LLM 看到
- 工具衰减从对话级改为轮次级
- 移除 chat() 末尾的 decay_tools()（已在 _on_turn_end 中每轮调用）

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## 任务 4：端到端验证

**文件：**
- 无新文件

- [ ] **步骤 14：重启服务**

```bash
# 清理 Python 缓存
find . -name "*.pyc" -delete
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 重启服务
go run main.go
```

- [ ] **步骤 15：测试"上网查新闻"场景**

在对话中输入"上网查一下今日热点新闻"，检查日志：

预期：
1. 第一轮：Skills 搜索可能 0 结果（正常，语义不匹配）
2. LLM 调用 `browser-server/browser_navigate`
3. 工具命中后，`hit_tool()` 产生 Pending Skills
4. **第二轮：`_on_turn_end()` 被调用，Pending Skills 被注入到 system_prompt**
5. LLM 在第二轮能看到 `browser-automation` skill

检查 stderr 日志中的关键行：
```
[ToolLifecycle] Found skills for browser-server/browser_navigate: ['browser-automation']
[Debug] Pending Skills: 1 results
```

---

## 验证清单

- [ ] 所有测试通过
- [ ] Pending Skills 在工具命中后的下一轮被注入
- [ ] 新工具 Schema 在下一轮对 LLM 可见
- [ ] 工具衰减每轮执行
- [ ] 不提供 on_turn_end 时行为不变（向后兼容）
- [ ] "上网查新闻"场景中 Skills 被注入
- [ ] 无 "tool call result does not follow tool call" 错误
- [ ] 无 "Tool function not found" 警告

---

## 解决方案覆盖矩阵

| 问题 | 严重程度 | 解决方案 | 任务 |
|------|---------|---------|------|
| 1. _inject_dynamic_resources 只调用一次 | HIGH | on_turn_end 每轮调用 | 2+3 |
| 2. Pending Skills 无法注入 | HIGH | _on_turn_end 中消费 Pending Skills | 2+3 |
| 3. 工具衰减不在多轮中生效 | MEDIUM | _on_turn_end 中每轮 decay_tools() | 3 |
| 4. 新工具 Schema 无法注入 | HIGH | _on_turn_end 中重新组装 tools_schema | 2+3 |
| 5. 向量检索只在第一轮执行 | MEDIUM | _on_turn_end 中重新执行 _inject_dynamic_resources | 3 |
| 6. 上下文不含循环内交互 | MEDIUM | _extract_context_from_messages 从 messages 提取 | 3 |
| 7. next_prompt_patcher 无法改 system_prompt | MEDIUM | on_turn_end 直接修改 messages[0] | 2 |
| 8. Pending Skills 时序错位 | LOW | on_turn_end 在每轮末尾消费 | 2+3 |
| 9. clear_pending_skills 在 finally 中 | LOW | 保留，但消费时机改为轮次级 | 3 |
| 10. hit_tool 反向引用 Runner | LOW | 不改，当前方案可接受 | - |
