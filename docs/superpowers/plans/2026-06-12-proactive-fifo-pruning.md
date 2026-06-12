# 上下文主动压缩 — 回调驱动 + 子Agent FIFO + 全场景溢出检测

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-step. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 主 Agent 在对话循环内检测到 prompt_tokens 超 80% 时，通过回调触发强制压缩，压缩完继续下一轮（循环不退出）；子 Agent 超 80% 时 FIFO 裁剪；统一溢出检测覆盖所有厂商。

**Architecture:**
- **主 Agent**：`prompt_tokens > 80%` → 调 `on_context_high_usage` 回调（阻塞压缩） → 回调返回 → 循环继续
- **子 Agent**：`prompt_tokens > 80%` → FIFO 裁剪到 50%（粗暴丢消息）
- **溢出检测**：统一函数覆盖 `isinstance(ContextWindowExceededError)` + 字符串模式 + HTTP 413

**Tech Stack:** Python, litellm

---

## 核心设计

### 循环不退出

agent_loop 检测到超阈值后，调回调函数执行压缩，回调跑完回来，继续下一轮。和 `on_turn_end` 回调一样的模式。循环绝对不退出。

### 主 Agent vs 子 Agent 的不同策略

用 `on_context_high_usage` 回调参数区分：
- 有回调 = 主 Agent，超阈值调回调（压缩）
- 回调为 None = 子 Agent，超阈值走 FIFO 裁剪

| | 主 Agent | 子 Agent |
|---|---------|---------|
| 超阈值动作 | 调 on_context_high_usage 回调 | FIFO 裁剪（丢旧消息） |
| 循环是否退出 | **不退出** | **不退出** |
| 保留信息 | 全部保留（压缩后摘要替代原文） | 丢弃旧消息（临时任务无妨） |

### 逻辑流程

**主 Agent：**
```
while turn < max_turns:
    1. 发送消息给 LLM
    2. 收到响应 → 提取 prompt_tokens
    3. 如果 prompt_tokens > 80%:
       → 调 on_context_high_usage(messages, last_prompt_tokens, context_window_tokens)
       → 回调内部执行压缩（entity-extractor → dream-evolver → context-manager）
       → 回调返回，从 DB 重新加载 messages
       → 继续下一轮
    4. 处理工具调用...
```

**子 Agent：**
```
while turn < max_turns:
    1. FIFO 裁剪（首轮用估算值，后续用 prompt_tokens）
    2. 发送消息给 LLM
    3. 收到响应 → 提取 prompt_tokens
    4. 处理工具调用...
```

### 第一轮保护

主 Agent 第一轮 `last_prompt_tokens=0`，不会触发回调。如果 history 很大，第一轮 LLM 可能报 `context_length_exceeded`，走已有的 CONTEXT_OVERFLOW 路径——这是正确的。

子 Agent 第一轮 `last_prompt_tokens=0`，走旧 `context_fifo_threshold` 回退保护。

### 上下文溢出的全部场景

| # | 场景 | 防线 | 覆盖 |
|---|------|------|------|
| 1 | 主 Agent prompt_tokens > 80% → 回调压缩 | Layer 1 | **本次新增** |
| 2 | 子 Agent prompt_tokens > 80% → FIFO 裁剪 | Layer 1 | **本次新增** |
| 3 | LLM API 返回 context_length_exceeded | Layer 2 | 已有 |
| 4 | litellm 抛 ContextWindowExceededError | Layer 2 | **本次补全覆盖** |
| 5 | Anthropic HTTP 413 | Layer 2 | **本次补全覆盖** |
| 6 | Qwen/DashScope 等自定义格式 | Layer 2 | **本次补全覆盖** |

## 修改文件

| 操作 | 文件 | 说明 |
|------|------|------|
| Modify | `agent/generic/llmcore.py:27-37` | MockResponse 增加 `usage` 参数 |
| Modify | `agent/generic/litellm_adapter.py` | 统一溢出检测函数 + 两处缺 usage 修复 |
| Modify | `agent/generic/agent_loop.py` | on_context_high_usage 回调 + prompt_tokens 检测 + 统一提取 |
| Modify | `agent/runner.py` | 传入 on_context_high_usage 回调 + 移除主Agent的 context_fifo_threshold |
| Modify | `agent/subagent.py` | 传 on_context_high_usage=None + context_target_threshold |
| Create | `tests/test_proactive_fifo.py` | TDD 测试（必须实际能运行） |

---

### Task 0: 验证测试基础设施（TDD第一步：先确保测试能跑）

**Files:**
- Create: `tests/test_proactive_fifo.py`

在做任何代码修改之前，先写一个最简单的测试验证 mock 方式正确。

- [ ] **Step 1: 写最小验证测试**

```python
"""最小验证测试：确保 agent_runner_loop 的 mock 方式正确"""
from unittest.mock import MagicMock
from agent.generic.agent_loop import agent_runner_loop, StreamEvent, StepOutcome
from agent.generic.llmcore import MockResponse


def _make_handler():
    """创建可用的 handler mock — dispatch 必须是生成器，next_prompt_patcher 必须返回字符串"""
    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []
    handler._current_messages = []
    handler.current_turn = 0

    def dispatch(tool_name, args, response, index=0):
        yield ""
        return StepOutcome(data=None, next_prompt="ok", should_exit=False)
    handler.dispatch = dispatch
    handler.next_prompt_patcher = lambda np, outcome, turn: np
    return handler


def _make_client(responses):
    """创建 mock client — chat 必须返回生成器且 StopIteration.value = 最后一个 response"""
    mock_client = MagicMock()
    def chat_fn(**kwargs):
        for resp in responses:
            yield resp
        return responses[-1] if responses else None
    mock_client.chat.return_value = chat_fn()
    return mock_client


def test_agent_loop_basic_no_tool_calls():
    """验证：agent_loop 能正常消费 mock client，返回 CURRENT_TASK_DONE"""
    handler = _make_handler()
    usage = {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050}
    mock_client = _make_client([
        MockResponse(thinking="", content="Hello!", tool_calls=[], raw="", usage=usage),
    ])

    results = []
    for event in agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="hi",
        handler=handler, tools_schema=[], max_turns=1, verbose=False,
    ):
        results.append(event)

    # 应该有 reply 事件
    reply_events = [e for e in results if isinstance(e, StreamEvent) and e.type == "reply"]
    assert len(reply_events) == 1
    assert "Hello!" in reply_events[0].content


def test_mockresponse_usage_parameter():
    """验证：MockResponse 接受 usage 参数"""
    usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
    resp = MockResponse(thinking="", content="test", tool_calls=[], raw="", usage=usage)
    assert resp.usage == usage
    resp2 = MockResponse(thinking="", content="test", tool_calls=[], raw="")
    # 当前 MockResponse 没有 usage 参数，这会报 AttributeError — 这是预期的
    # Task 1 修改后这个测试才能通过
```

- [ ] **Step 2: 运行验证测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_proactive_fifo.py::test_agent_loop_basic_no_tool_calls -v
```

必须先通过这个测试，才能继续。如果失败，修复 mock 方式直到通过。

- [ ] **Step 3: 提交**

```bash
git add tests/test_proactive_fifo.py
git commit -m "test: verify test infrastructure — mock handler/client for agent_runner_loop"
```

---

### Task 1: MockResponse 增加 usage 参数 + litellm_adapter 统一溢出检测

**Files:**
- Modify: `agent/generic/llmcore.py:27-37`
- Modify: `agent/generic/litellm_adapter.py`

- [ ] **Step 1: MockResponse 增加 usage 参数**

在 `agent/generic/llmcore.py` MockResponse.__init__ 增加 `usage=None` 参数，赋值 `self.usage = usage`。

- [ ] **Step 2: litellm_adapter.py 添加统一溢出检测函数**

在 import 区域后添加 `_is_context_overflow_error` 函数和 `_OVERFLOW_PATTERNS` 列表。三层检测：isinstance(ContextWindowExceededError) > HTTP 413 > 字符串匹配。

- [ ] **Step 3: 替换两处重复的 is_context_overflow 检测代码**

改为调用 `_is_context_overflow_error(exc)`。

- [ ] **Step 4: 修复两处 context_overflow 路径的 usage 缺失**

两处 `MockResponse(..., context_overflow=True)` 都补上 `usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}`。

- [ ] **Step 5: 语法验证 + 运行测试 + 提交**

```bash
python -c "import ast; ast.parse(open('agent/generic/llmcore.py').read()); ast.parse(open('agent/generic/litellm_adapter.py').read()); print('OK')"
python -m pytest tests/test_proactive_fifo.py::test_mockresponse_usage_parameter -v
git add agent/generic/llmcore.py agent/generic/litellm_adapter.py tests/test_proactive_fifo.py
git commit -m "feat: MockResponse usage param + unified context overflow detection + fix usage=None"
```

---

### Task 2: agent_loop — on_context_high_usage 回调 + prompt_tokens 检测

**Files:**
- Modify: `agent/generic/agent_loop.py`

核心变更：
1. 增加 `on_context_high_usage` 回调参数
2. 主 Agent：prompt_tokens > 80% → 调回调，回调完从 DB 重新加载 messages，继续下一轮
3. 子 Agent：prompt_tokens > 80% → FIFO 裁剪
4. prompt_tokens 提取移到 harness continue 之前
5. 旧 FIFO 只在 last_prompt_tokens==0 时执行（首轮保护）

- [ ] **Step 1: 修改函数签名，增加 on_context_high_usage 参数**

在 `agent_runner_loop` 函数签名中，在 `context_fifo_threshold` 之后添加：

```python
    context_target_threshold=0,  # FIFO 裁剪目标 token 量
    on_context_high_usage=None,  # 主Agent超阈值回调；None=子Agent走FIFO
```

- [ ] **Step 2: 初始化 last_prompt_tokens**

在 `turn = 0` 行之后添加 `last_prompt_tokens = 0`。

- [ ] **Step 3: 替换 FIFO + 使用率检查逻辑**

将第 183-208 行替换为：

```python
        # === 上下文使用率检测 ===
        if last_prompt_tokens > 0 and context_window_tokens > 0:
            usage_ratio = last_prompt_tokens / context_window_tokens
            if usage_ratio > warning_threshold:
                if on_context_high_usage:
                    # 主 Agent：调回调执行压缩，循环不退出
                    logger.info(f"[Context] Proactive compress: {last_prompt_tokens}/{context_window_tokens} tokens "
                                f"({usage_ratio:.1%} > {warning_threshold:.0%})")
                    on_context_high_usage(messages, last_prompt_tokens, context_window_tokens)
                    # 回调内部已完成：4步子Agent压缩 + 执行compress_plan + 重新加载messages
                    # 回调会原地修改 messages 列表（messages[:] = ...）
                    last_prompt_tokens = 0  # 重置，下轮重新获取
                else:
                    # 子 Agent：FIFO 裁剪到 target 阈值
                    target_tokens = context_target_threshold if context_target_threshold > 0 else int(context_window_tokens * 0.50)
                    if len(messages) > 2:
                        removed = 0
                        current_tokens = count_messages_tokens(messages)
                        while len(messages) > 2 and current_tokens > target_tokens:
                            first = messages[2]
                            messages.pop(2)
                            removed += 1
                            if first.get("role") == "assistant" and first.get("tool_calls"):
                                while len(messages) > 2 and messages[2].get("role") == "tool":
                                    messages.pop(2)
                                    removed += 1
                            current_tokens = count_messages_tokens(messages)
                        if removed > 0:
                            logger.info(f"[FIFO] Proactive pruning: {last_prompt_tokens}/{context_window_tokens} tokens "
                                        f"({usage_ratio:.1%} > {warning_threshold:.0%}), removed {removed} messages, "
                                        f"now ~{current_tokens} tokens (target {target_tokens})")
        # 旧 FIFO 回退：只在首轮（last_prompt_tokens==0）时执行
        if context_fifo_threshold > 0 and len(messages) > 2 and last_prompt_tokens == 0:
            current_tokens = count_messages_tokens(messages)
            if current_tokens > context_fifo_threshold:
                removed = 0
                while len(messages) > 2 and current_tokens > context_fifo_threshold:
                    first = messages[2]
                    messages.pop(2)
                    removed += 1
                    if first.get("role") == "assistant" and first.get("tool_calls"):
                        while len(messages) > 2 and messages[2].get("role") == "tool":
                            messages.pop(2)
                            removed += 1
                    current_tokens = count_messages_tokens(messages)
                if removed > 0:
                    logger.info(f"[FIFO] Fallback truncation: removed {removed} oldest messages, "
                                f"tokens {current_tokens}/{context_fifo_threshold}")
```

注意：回调中 messages 的重新加载方式需要在实施时确认——agent_loop 是同步的，需要用同步方式读 DB。具体实现由实施者根据实际 API 确定。

- [ ] **Step 4: prompt_tokens 提取**

verbose=False 路径：在 `response = exhaust(response_gen)` 之后立即提取。
verbose=True 路径：在 `yield StreamEvent("system", "\n\n")` 之后提取。

```python
if hasattr(response, 'usage') and response.usage:
    last_prompt_tokens = response.usage.get('prompt_tokens', 0) if isinstance(response.usage, dict) else getattr(response.usage, 'prompt_tokens', 0)
```

- [ ] **Step 5: 语法验证 + 提交**

```bash
python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); print('OK')"
git add agent/generic/agent_loop.py
git commit -m "feat: on_context_high_usage callback + prompt_tokens detection in loop (no exit)"
```

---

### Task 3: runner.py — 传入 on_context_high_usage 回调 + 移除主 Agent FIFO

**Files:**
- Modify: `agent/runner.py`

- [ ] **Step 1: 添加导入**

```python
from agent.subagent import _read_target_threshold
```

- [ ] **Step 2: 定义回调函数**

在 NiuRunner 类中定义 `_on_context_high_usage` 方法。回调内部完成全部工作：4步子Agent压缩 + 执行compress_plan + 重新加载messages。原地修改 messages 列表。

```python
def _on_context_high_usage(self, messages, tokens_used, tokens_limit):
    """主 Agent 上下文超阈值回调 — 执行完整 force 压缩流程（阻塞，循环不退出）
    
    回调完成后原地修改 messages 列表（从 DB 重新加载压缩后的消息）。
    agent_loop 不需要知道 DB、不需要导入 niu_api 的任何东西。
    """
    from agent.subagent import call_subagent
    from agent.runner import is_stop_requested
    import concurrent.futures
    logger.info(f"[Runner] Proactive compress: {tokens_used}/{tokens_limit} tokens")

    try:
        # === 1/4: entity-extractor（全量）===
        if is_stop_requested():
            return
        entity_task = self._build_entity_extractor_task()  # 参考 compat.py:1439-1446 构建
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(call_subagent, "entity-extractor", entity_task, self.llm_config, None, None)
            entity_result = future.result(timeout=120)
        self._process_entity_cursor(entity_result)  # 游标写入 ~/.niu/last_entity_extract.json

        # === 2/4: dream-evolver（增量）===
        if is_stop_requested():
            return
        dream_task = self._build_dream_evolver_task()  # 参考 compat.py:1533-1538 构建
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(call_subagent, "dream-evolver", dream_task, self.llm_config, None)
            dream_result = future.result(timeout=120)
        self._process_dream_cursor(dream_result)  # 游标写入 ~/.niu/last_dream_evolve.json

        # === 2.5/4: journal-agent（增量）===
        if is_stop_requested():
            return
        journal_task = self._build_journal_task()  # 参考 compat.py:1626-1634 构建
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(call_subagent, "journal-agent", journal_task, self.llm_config, None)
            journal_result = future.result(timeout=120)
        self._process_journal_cursor(journal_result)  # 游标写入 ~/.niu/last_journal.json

        # === 3/4: context-manager（force 模式，一轮 JSON 方案）===
        if is_stop_requested():
            return
        cm_task = self._build_context_manager_task(tokens_used, tokens_limit)  # 参考 compat.py:1714-1741 构建
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(call_subagent, "context-manager", cm_task, self.llm_config, None, None, None, 0)
            cm_result = future.result(timeout=120)

        # === 执行 compress_plan.json ===
        # 参考 compat.py:1768-1887：读取 plan.json，验证 ID，执行 deletes/updates
        self._execute_compress_plan()  # 通过 run_coroutine_threadsafe 桥接 async DB 操作

        # === 从 DB 重新加载 messages ===
        fresh_msgs = self._sync_reload_messages()  # 通过 run_coroutine_threadsafe 桥接
        if fresh_msgs:
            messages[:] = [messages[0]] + fresh_msgs  # 原地修改，确保 handler._current_messages 也更新

        logger.info("[Runner] Proactive compress completed")
    except Exception as e:
        logger.error(f"[Runner] Proactive compress failed: {e}")
```

关键设计决策：
1. **每步用 concurrent.futures.ThreadPoolExecutor + timeout=120** — 防止子Agent永久阻塞
2. **每步前检查 is_stop_requested()** — 用户按 Stop 后立即退出，不再启动下一个子Agent
3. **原地修改 messages[:] = ...** — 不是重绑定，确保 handler._current_messages 等引用也更新
4. **_build_xxx_task() / _process_xxx_cursor()** — 封装 task 构建和游标管理，参考 compat.py 对应代码
5. **_execute_compress_plan()** — 读取 compress_plan.json，通过 run_coroutine_threadsafe 执行 async DB 删除/更新
6. **_sync_reload_messages()** — 通过 run_coroutine_threadsafe 从 DB 重新加载消息（和 _sync_add_message 同一模式）

注意：_build_xxx_task() 方法需要参考 compat.py:1429-1741 的代码构建完整的 task 字符串（包含消息文本、游标、token 预算等）。这是最大的工作量，但模式已确定——从 compat.py 搬过来改成同步调用即可。

- [ ] **Step 3: 传递参数给 agent_runner_loop**

移除 `context_fifo_threshold=int(context_window_tokens * 0.75)`，替换为：

```python
                context_target_threshold=int(context_window_tokens * _read_target_threshold()),
                on_context_high_usage=self._on_context_high_usage,
```

- [ ] **Step 4: 语法验证 + 提交**

```bash
python -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"
git add agent/runner.py
git commit -m "feat: main agent uses on_context_high_usage callback instead of FIFO"
```

---

### Task 4: subagent.py — 传 on_context_high_usage=None + context_target_threshold

**Files:**
- Modify: `agent/subagent.py`

- [ ] **Step 1: _run_agent_loop 函数签名增加参数**

```python
def _run_agent_loop(
    ...
    context_target_threshold: int = 0,
    ...
) -> Tuple[str, Any]:
```

- [ ] **Step 2: _run_agent_loop 内部传递给 agent_runner_loop**

```python
    gen = agent_runner_loop(
        ...
        context_target_threshold=context_target_threshold,
        on_context_high_usage=None,  # 子 Agent：超阈值走 FIFO
        ...
    )
```

- [ ] **Step 3: call_subagent 计算并传递 context_target_threshold**

```python
    target_threshold = _read_target_threshold()
    context_target_threshold_val = int(context_window_tokens * target_threshold)

    result_text, return_value = _run_agent_loop(
        ...
        context_target_threshold=context_target_threshold_val,
        ...
    )
```

- [ ] **Step 4: 语法验证 + 提交**

```bash
python -c "import ast; ast.parse(open('agent/subagent.py').read()); print('OK')"
git add agent/subagent.py
git commit -m "feat: sub-agent uses FIFO (on_context_high_usage=None) + context_target_threshold"
```

---

### Task 5: TDD 测试 — 完整测试

**Files:**
- Modify: `tests/test_proactive_fifo.py`

在 Task 0 的基础设施之上，添加完整测试。

- [ ] **Step 1: 写完整测试**

```python
# 在 Task 0 的 _make_handler / _make_client 之后添加：

def test_main_agent_calls_callback_on_high_usage():
    """主 Agent prompt_tokens > 80% → 调用 on_context_high_usage 回调，循环不退出"""
    handler = _make_handler()
    callback_called = {"count": 0, "args": None}

    def my_callback(messages, tokens, limit):
        callback_called["count"] += 1
        callback_called["args"] = (tokens, limit)
        # 模拟压缩：不实际改 messages，让循环继续

    usage_high = {"prompt_tokens": 170000, "completion_tokens": 500, "total_tokens": 170500}
    usage_normal = {"prompt_tokens": 90000, "completion_tokens": 200, "total_tokens": 90200}

    mock_client = MagicMock()
    def chat_fn1(**kwargs):
        yield MockResponse(thinking="", content="OK", tool_calls=[], raw="", usage=usage_high)
        return MockResponse(thinking="", content="OK", tool_calls=[], raw="", usage=usage_high)
    def chat_fn2(**kwargs):
        yield MockResponse(thinking="", content="Done", tool_calls=[], raw="", usage=usage_normal)
        return MockResponse(thinking="", content="Done", tool_calls=[], raw="", usage=usage_normal)
    mock_client.chat.side_effect = [chat_fn1(), chat_fn2()]

    results = []
    for event in agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=2, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=my_callback,
    ):
        results.append(event)

    assert callback_called["count"] >= 1, f"Callback should be called, got {callback_called['count']}"
    assert callback_called["args"][0] == 170000
    # 循环应该继续（不退出）
    assert mock_client.chat.call_count == 2


def test_sub_agent_fifo_pruning():
    """子 Agent prompt_tokens > 80% → FIFO 裁剪（不调回调）"""
    handler = _make_handler()
    callback_called = {"count": 0}
    def my_callback(messages, tokens, limit):
        callback_called["count"] += 1

    usage_high = {"prompt_tokens": 170000, "completion_tokens": 500, "total_tokens": 170500}
    usage_normal = {"prompt_tokens": 90000, "completion_tokens": 200, "total_tokens": 90200}

    mock_client = MagicMock()
    def chat_fn1(**kwargs):
        yield MockResponse(thinking="", content="OK", tool_calls=[], raw="", usage=usage_high)
        return MockResponse(thinking="", content="OK", tool_calls=[], raw="", usage=usage_high)
    def chat_fn2(**kwargs):
        yield MockResponse(thinking="", content="Done", tool_calls=[], raw="", usage=usage_normal)
        return MockResponse(thinking="", content="Done", tool_calls=[], raw="", usage=usage_normal)
    mock_client.chat.side_effect = [chat_fn1(), chat_fn2()]

    big_history = [{"role": "user", "content": "x" * 2000}, {"role": "assistant", "content": "y" * 2000}] * 40

    results = []
    for event in agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=2, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=None,
        history=big_history,
    ):
        results.append(event)

    # 子 Agent 不调回调
    assert callback_called["count"] == 0
    # 第二轮调用时 messages 应该被 FIFO 裁剪
    assert mock_client.chat.call_count == 2
    second_call_messages = mock_client.chat.call_args_list[1][1].get("messages")
    assert len(second_call_messages) < 60, f"Expected pruning, got {len(second_call_messages)}"


def test_no_pruning_when_below_warning():
    """prompt_tokens < 80% 时不裁剪也不调回调"""
    handler = _make_handler()
    callback_called = {"count": 0}
    def my_callback(messages, tokens, limit):
        callback_called["count"] += 1

    usage_low = {"prompt_tokens": 100000, "completion_tokens": 500, "total_tokens": 100500}
    mock_client = _make_client([
        MockResponse(thinking="", content="OK", tool_calls=[], raw="", usage=usage_low),
    ])

    for event in agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=1, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=my_callback,
    ):
        pass

    assert callback_called["count"] == 0


def test_context_overflow_still_works():
    """context_overflow 标记仍然触发 CONTEXT_OVERFLOW"""
    handler = _make_handler()
    mock_client = _make_client([
        MockResponse(thinking="", content="", tool_calls=[], raw="",
                      context_overflow=True, usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
    ])

    results = []
    for event in agent_runner_loop(
        client=mock_client, system_prompt="test", user_input="test",
        handler=handler, tools_schema=[], max_turns=1, verbose=False,
        context_window_tokens=200000, context_fifo_threshold=0,
        context_target_threshold=100000, on_context_high_usage=lambda m, t, l: None,
    ):
        results.append(event)

    overflow_returns = [r for r in results if isinstance(r, dict) and r.get("result") == "CONTEXT_OVERFLOW"]
    assert len(overflow_returns) > 0


def test_is_context_overflow_error_all_patterns():
    """验证 _is_context_overflow_error 覆盖所有已知模式"""
    from agent.generic.litellm_adapter import _is_context_overflow_error

    assert _is_context_overflow_error(Exception("context_length_exceeded"))
    assert _is_context_overflow_error(Exception("maximum context length"))
    assert _is_context_overflow_error(Exception("prompt is too long"))
    assert _is_context_overflow_error(Exception("prompt: length"))
    assert _is_context_overflow_error(Exception("exceed context limit"))
    assert _is_context_overflow_error(Exception("is longer than the model's context length"))
    assert _is_context_overflow_error(Exception("input tokens exceed the configured limit"))
    assert _is_context_overflow_error(Exception("exceeds the maximum number of tokens"))
    assert _is_context_overflow_error(Exception("input is too long"))
    assert _is_context_overflow_error(Exception("context window exceeded"))
    assert not _is_context_overflow_error(Exception("rate limit exceeded"))
    assert not _is_context_overflow_error(Exception("internal server error"))
```

- [ ] **Step 2: 运行全部测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_proactive_fifo.py -v
```

必须全部通过。如果失败，修复直到通过。

- [ ] **Step 3: 提交**

```bash
git add tests/test_proactive_fifo.py
git commit -m "test: full TDD tests for callback-driven compress + sub-agent FIFO + overflow detection"
```

---

### Task 6: 真实集成测试

- [ ] **Step 1: 临时提交备份**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && git add -A && git commit -m "backup: before proactive compress integration test"
```

- [ ] **Step 2: 启动程序，验证场景**

1. 与妞妞长对话，观察状态栏上下文百分比
2. 当百分比超过 80% 时，确认日志出现 `Proactive compress`
3. 确认压缩流程启动（entity-extractor → dream-evolver → context-manager）
4. 压缩完成后，确认对话自动继续（不需要用户重发消息）
5. 确认压缩后上下文百分比回落

- [ ] **Step 3: 检查日志**

```bash
grep -i "Proactive compress\|FIFO.*Proactive\|context_overflow" /tmp/niu_test_stderr.log | tail -30
```
