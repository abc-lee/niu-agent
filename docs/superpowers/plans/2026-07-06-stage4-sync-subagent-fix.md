# 阶段四同步子 Agent @niu-agent 询问路径修复 Implementation Plan (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复同步子 Agent @niu-agent 询问路径的根因——同步子 Agent 用随机 hex 命名导致 LLM 记不住 + 主 Agent content 误回复时无反馈，让同步 @niu-agent 询问路径端到端走通。

**Architecture:** 
- **方案 B（命名修复）**：`SubagentRegistry.register()` 加 `force_unique_name` 参数，同步路径传 agent_name（如 `browser-operator`），异步路径保持随机 hex 后缀；同时修复 3 个连带 BLOCKER——`_extract_unique_name` 正则支持纯 agent_name 格式（程序触发路径 `call_subagent_with_auto_answer` 依赖）、`_call_subagent_gen` 在 LLM 不传 unique_name 时 fallback 到 agent_name（让第三分支能进入）、schema 描述 + niu.md 文档更新。
- **方案 C（最简拦截）**：主 Agent content 误回复本质和 MCP 工具调用是同一层——都是程序在处理主 Agent 的输出。直接在 `_intercept_at_prefix_content` 改 L83 条件让主 Agent 也进拦截层，加几行检测：主 Agent + content 以 `@<同步挂起子名>` 开头 + 无 tool_calls 时，append assistant content + user 错误提示，返回现有 `FORMAT_ERROR` 常量——agent_runner_loop 已有 `continue` 逻辑（L621-623）让 LLM 重做。**不加新常量、不加新函数、不改 agent_runner_loop**。

**Tech Stack:** Python 3.11+, pytest, threading.Lock, secrets.token_hex, re

---

## v3 修订说明（基于审查 Agent + 验证 Agent + 用户反馈）

v1 计划有 6 个 BLOCKER 级漏洞，v2 修复但方案 C 过度复杂（新增 MAIN_AGENT_FORMAT_ERROR 常量 + 新增 helper 函数 + 改 agent_runner_loop）。v3 按用户反馈"程序直接返回错误给主 Agent，它自然进入下一轮"简化方案 C 到最小——复用现有 FORMAT_ERROR 机制，只改 1 个条件 + 加几行检测。

### v1 BLOCKER 清单（已验证属实）

| # | BLOCKER | v2 修复 Task |
|---|---------|--------------|
| B1 | `_extract_unique_name` 正则严格匹配 4 位 hex，方案 B 后 `[browser-operator] 问题` 不匹配，`call_subagent_with_auto_answer` 把问题当正常结果返回 | Task 3 |
| B2 | schema `required: []`，LLM 不传 unique_name 时 `handler.py:1007` answer_unique_name=None，第三分支进不去，落到同步新任务分支触发 ValueError 或错误新建空 task session | Task 4 |
| B3 | 方案 C 推 MainAgentRequestQueue，但链路 A `_drain_main_agent_request_queue` 检查 `if _chat_lock.locked(): return`，主 Agent chat 期间不推送 | Task 5（重构方案 C）|
| B4 | 主 Agent chat 结束 finally 块调 `cleanup_suspended_sync_subagents` 清理挂起 session，错误反馈到达时 session 已不存在 | Task 5（重构方案 C）|
| B5 | `config/agents/niu.md` 未定义 `[system]` 前缀语义，主 Agent 收到也无法识别 | Task 5（重构方案 C，不再依赖 [system] 前缀）|
| B6 | `_call_subagent_gen` 无 unique_name fallback 到 agent_name 的逻辑 | Task 4 |

### v3 核心简化：方案 C 复用现有 FORMAT_ERROR 机制

v2 方案 C 新增 `MAIN_AGENT_FORMAT_ERROR` 常量 + 新增 `_check_main_agent_content_reply_to_suspended` 函数 + 改 agent_runner_loop 处理新常量——过度复杂。用户反馈："程序直接返回错误给主 Agent，它自然会进入下一轮会话，为什么还要有这么多其他的处理逻辑？"

v3 简化：主 Agent content 误回复本质和 MCP 工具调用是同一层——都是程序在处理主 Agent 的输出。`_intercept_at_prefix_content` 已经是"content 意图识别"入口，已经有 `FORMAT_ERROR` 常量 + agent_runner_loop 已有 `continue` 重试逻辑（L621-623）。只需：
1. 改 L83 条件：让主 Agent（`memory_context is None and not is_sync_subagent`）也进拦截层（之前直接 NO_INTERCEPTION）
2. 在函数开头加几行检测：主 Agent + content 以 `@<同步挂起子名>` 开头 + 无 tool_calls → append 错误提示 + 返回 `FORMAT_ERROR`
3. 不命中就 `NO_INTERCEPTION`（走原逻辑）

不加新常量、不加新函数、不改 agent_runner_loop。

## 失败根因复盘（真实日志 000040-000046）

- 000044：子 Agent 正确输出 `@niu-agent 第一个问题是：...`，拦截层包装成 `[browser-operator-708b] 问题` 返回主 Agent ✅
- 000045：主 Agent 失败——思考过程识别了场景，但复用异步路径的 content 写法：`@browser-operator-708b 好的！第一个问题，我选择：2. ...`，没调 `chat-with-browser-operator` 工具 ❌
- 000046+：主 Agent yield content 给前端，子 Agent 永久挂起在 waiting_for_answer，registry 残留

**根因 1（命名错误）**：`SubagentRegistry._gen_unique_name`（subagent_registry.py:47-53）对所有路径都用 `<agent_type>-<4位hex>`。同步路径沿用了异步路径设计——主 Agent 工具名是 `chat-with-browser-operator`，但 unique_name 是 `browser-operator-708b`，LLM 必须在 `unique_name` 参数填 `browser-operator-708b`，记不住。

**根因 2（无反馈）**：主 Agent content 误回复时无任何反馈。v1 想用 db_monitor 推 MainAgentRequestQueue，但链路 A 在 _chat_lock 持有期间不推送，cleanup 又先清理挂起 session——绕路走不通。v2 改为主 Agent 工具循环内直接拦截。

## 修复策略

### 方案 B（命名修复）—— 4 个子任务

1. **Task 1**：`SubagentRegistry.register()` 加 `force_unique_name` 参数 + 同名冲突检测
2. **Task 2**：`call_subagent` 同步路径传 `force_unique_name=agent_name`
3. **Task 3**：`_extract_unique_name` 正则支持纯 agent_name 格式（修 B1）
4. **Task 4**：`_call_subagent_gen` 在 LLM 不传 unique_name 时 fallback 到 agent_name + schema 描述 + niu.md 文档更新（修 B2/B6）

### 方案 C 最简拦截（复用 FORMAT_ERROR）—— 1 个子任务

5. **Task 5**：`_intercept_at_prefix_content` 改 L83 条件让主 Agent 进拦截层 + 加几行检测误回复模式返回 FORMAT_ERROR（修 B3/B4/B5，复用现有常量和 agent_runner_loop continue 逻辑）

### 验证 —— 2 个子任务

6. **Task 6**：端到端 mock 测试
7. **Task 7**：全量测试 + 真实 LLM 端到端验证 + 更新记忆

## File Structure

| 文件 | 改动 | 任务 |
|------|------|------|
| `agent/subagent_registry.py` | `register()` 加 `force_unique_name` 参数 + 同名检测 | Task 1 |
| `agent/subagent.py` L848 | 同步路径传 `force_unique_name=agent_name` | Task 2 |
| `agent/subagent.py` L948-955 | `_extract_unique_name` 正则支持纯 agent_name | Task 3 |
| `agent/handler.py` L1006-1008 | `_call_subagent_gen` 在 unique_name 为 None 时 fallback 到 agent_name | Task 4 |
| `agent/runner.py` L397-401 | schema 描述更新（unique_name 可省略，默认用 agent_name） | Task 4 |
| `config/agents/niu.md` | 文档更新（同步子 Agent unique_name = agent_name） | Task 4 |
| `agent/generic/agent_loop.py` L56-136 | `_intercept_at_prefix_content` 改 L83 条件 + 加主 Agent 误回复检测返回 FORMAT_ERROR | Task 5 |
| `tests/test_subagent_registry_async.py` | 加 force_unique_name + 同名冲突测试 | Task 1 |
| `tests/test_sync_subagent_interaction.py` | 加同步路径命名 + 第二次接续 + 端到端测试 | Task 2/6 |
| `tests/test_call_subagent_with_auto_answer.py` | 加同步路径正则匹配测试 | Task 3 |
| `tests/test_at_prefix_interception.py` | 加主 Agent 误用 content 拦截测试 | Task 5 |

---

## Task 1: SubagentRegistry.register 加 force_unique_name 参数

**Files:**
- Modify: `agent/subagent_registry.py:47-79`
- Test: `tests/test_subagent_registry_async.py`

- [ ] **Step 1: 写失败测试——force_unique_name 透传 + 同名冲突 + 不传仍用随机 hex**

在 `tests/test_subagent_registry_async.py` 末尾加：

```python
def test_register_with_force_unique_name():
    """force_unique_name 透传：register 用指定名字而非随机 hex"""
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    name = SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    assert name == "browser-operator"
    instance = SubagentRegistry.get(name)
    assert instance is not None
    assert instance.agent_type == "browser-operator"
    SubagentRegistry.unregister(name)


def test_register_force_unique_name_conflict():
    """force_unique_name 同名冲突 → 抛 ValueError（同步路径同类型只能跑一个）"""
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq1 = SubagentSupplementQueue(unique_name="")
    name1 = SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq1,
        force_unique_name="browser-operator",
    )
    assert name1 == "browser-operator"

    sq2 = SubagentSupplementQueue(unique_name="")
    try:
        SubagentRegistry.register(
            agent_type="browser-operator",
            supplement_queue=sq2,
            force_unique_name="browser-operator",
        )
        assert False, "应抛 ValueError（同名冲突）"
    except ValueError as e:
        assert "browser-operator" in str(e)
        assert "已在运行" in str(e) or "已存在" in str(e)
    finally:
        SubagentRegistry.unregister(name1)


def test_register_without_force_unique_name_uses_random_hex():
    """不传 force_unique_name → 仍用随机 hex 后缀（异步路径保持原逻辑）"""
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    name = SubagentRegistry.register(
        agent_type="file-processor",
        supplement_queue=sq,
        is_sync=False,
    )
    assert name.startswith("file-processor-")
    assert len(name) == len("file-processor-") + 4  # 4 位 hex 后缀
    SubagentRegistry.unregister(name)
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_subagent_registry_async.py::test_register_with_force_unique_name tests/test_subagent_registry_async.py::test_register_force_unique_name_conflict tests/test_subagent_registry_async.py::test_register_without_force_unique_name_uses_random_hex -v 2>&1 | tail -20
```

Expected: 三个测试 FAIL（`force_unique_name` 参数不存在，TypeError）。

- [ ] **Step 3: 修改 register 签名加 force_unique_name**

`agent/subagent_registry.py:55-79` 改为：

```python
    @classmethod
    def register(
        cls,
        agent_type: str,
        supplement_queue: Any,
        memory_context: Optional[Any] = None,
        is_sync: bool = True,
        task: Optional[Union[asyncio.Task, ConcurrentFuture]] = None,
        force_unique_name: Optional[str] = None,
    ) -> str:
        """注册一个子 Agent，返回唯一名。

        同步子 Agent：is_sync=True，task=None，memory_context=None
        异步子 Agent：is_sync=False，task=asyncio.Task 或 concurrent.futures.Future，memory_context=SubagentMemoryContext

        Args:
            force_unique_name: 指定 unique_name（同步路径用 agent_name，避免 LLM 记随机 hex 后缀）。
                              None 时用 _gen_unique_name 生成随机 hex 后缀（异步路径保持原逻辑）。
                              同名已存在时抛 ValueError（同步路径同类型只能跑一个）。
        """
        with cls._lock:
            if force_unique_name is not None:
                if force_unique_name in cls._instances:
                    raise ValueError(
                        f"子 Agent {force_unique_name} 已在运行（同步路径同类型只能跑一个），"
                        f"请先用 chat-with-{force_unique_name} 回复当前挂起的 session 或停止它"
                    )
                name = force_unique_name
            else:
                name = cls._gen_unique_name(agent_type)
            cls._instances[name] = RunningSubagent(
                unique_name=name,
                agent_type=agent_type,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
                is_sync=is_sync,
                task=task,
            )
            return name
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_subagent_registry_async.py::test_register_with_force_unique_name tests/test_subagent_registry_async.py::test_register_force_unique_name_conflict tests/test_subagent_registry_async.py::test_register_without_force_unique_name_uses_random_hex -v 2>&1 | tail -20
```

Expected: 三个测试 PASS。

- [ ] **Step 5: 跑全量 registry 测试确认无回归**

```bash
python -m pytest tests/test_subagent_registry_async.py -v 2>&1 | tail -30
```

Expected: 所有测试 PASS。

- [ ] **Step 6: Commit**

```bash
git add agent/subagent_registry.py tests/test_subagent_registry_async.py
git commit -m "$(cat <<'EOF'
feat(subagent_registry): register 加 force_unique_name 参数

- 同步路径用 agent_name 作 unique_name（如 browser-operator），避免 LLM 记随机 hex 后缀
- 异步路径保持 _gen_unique_name 随机 hex 后缀（多并发 + db_monitor 路由需要）
- force_unique_name 同名冲突抛 ValueError（同步路径同类型只能跑一个）
- 加 3 个测试覆盖：force 透传 / 同名冲突 / 不传 force 仍用随机 hex

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: call_subagent 同步路径传 force_unique_name=agent_name

**Files:**
- Modify: `agent/subagent.py:844-854`
- Test: `tests/test_sync_subagent_interaction.py`

- [ ] **Step 1: 写失败测试——同步路径 unique_name 等于 agent_name + 第二次接续**

在 `tests/test_sync_subagent_interaction.py` 加测试：

```python
def test_call_subagent_sync_uses_agent_name_as_unique_name(monkeypatch):
    """同步路径 unique_name 等于 agent_name（无随机 hex 后缀）"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry

    captured_unique_names = []

    def fake_run_agent_loop(**kwargs):
        handler = kwargs["handler"]
        captured_unique_names.append(getattr(handler, "_subagent_unique_name", ""))
        return "子 Agent 完成", {"result": "EXITED", "messages": [], "finish_reason": "exited"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(subagent, "get_tools_schema", lambda **kw: [])
    monkeypatch.setattr(subagent, "_build_subagent_tools_schema", lambda **kw: [])

    try:
        subagent.call_subagent(
            agent_name="browser-operator",
            task="测试任务",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
        assert captured_unique_names == ["browser-operator"], f"unique_name 应为 browser-operator，实际：{captured_unique_names}"
    finally:
        for name in captured_unique_names:
            SubagentRegistry.unregister(name)


def test_call_subagent_sync_second_call_with_answer_resumes_suspended_session(monkeypatch):
    """第二次 call_subagent 传 answer + answer_unique_name=agent_name 能进入第三分支恢复 session"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    # 先注册一个同步挂起 session（模拟第一次 call_subagent 挂起的状态）
    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    instance = SubagentRegistry.get("browser-operator")
    instance.state = "waiting_for_answer"
    instance.suspended_messages = [{"role": "system", "content": "挂起的 messages"}]
    instance.suspended_handler = None
    instance.suspended_client = None
    instance.suspended_tools_schema = []
    instance.suspended_system_message = None

    # mock _run_agent_loop 第二次调用（回复路径）返回正常结束
    resumed_messages_seen = []
    def fake_run_agent_loop(**kwargs):
        resumed_messages_seen.append(kwargs.get("resumed_messages"))
        return "子 Agent 完成", {"result": "EXITED", "messages": [], "finish_reason": "exited"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(subagent, "get_tools_schema", lambda **kw: [])
    monkeypatch.setattr(subagent, "_build_subagent_tools_schema", lambda **kw: [])

    try:
        result = subagent.call_subagent(
            agent_name="browser-operator",
            task="",
            answer="@browser-operator 我选择 2",
            answer_unique_name="browser-operator",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
        # 第三分支应进入并恢复 session（不应报错）
        assert "[错误]" not in result, f"不应报错，实际：{result}"
        # resumed_messages 应被透传（含挂起的 messages + 主 Agent 回答）
        assert len(resumed_messages_seen) == 1
        resumed = resumed_messages_seen[0]
        assert resumed is not None
        # 最后一条应是 [主 Agent 回答]
        assert "[主 Agent 回答]" in resumed[-1]["content"]
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_call_subagent_sync_second_call_same_agent_name_conflict(monkeypatch):
    """同步路径同类型已在跑 + 第二次不传 answer → 报错提示用 answer 参数"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    instance = SubagentRegistry.get("browser-operator")
    instance.state = "waiting_for_answer"

    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(subagent, "get_tools_schema", lambda **kw: [])
    monkeypatch.setattr(subagent, "_build_subagent_tools_schema", lambda **kw: [])

    try:
        result = subagent.call_subagent(
            agent_name="browser-operator",
            task="第二个任务",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
        assert "[错误]" in result
        assert "chat-with-browser-operator" in result or "已在运行" in result
    finally:
        SubagentRegistry.unregister("browser-operator")
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
python -m pytest tests/test_sync_subagent_interaction.py::test_call_subagent_sync_uses_agent_name_as_unique_name tests/test_sync_subagent_interaction.py::test_call_subagent_sync_second_call_with_answer_resumes_suspended_session tests/test_sync_subagent_interaction.py::test_call_subagent_sync_second_call_same_agent_name_conflict -v 2>&1 | tail -20
```

Expected: 第一个测试 FAIL（unique_name 仍是 `browser-operator-708b`）；第二/三个测试可能 FAIL（取决于原逻辑）。

- [ ] **Step 3: 修改 call_subagent 同步路径传 force_unique_name**

`agent/subagent.py:844-887` 的同步新任务分支改为：

```python
    else:
        # 同步路径：用 agent_name 作 unique_name（避免 LLM 记随机 hex 后缀）
        if supplement_queue is None:
            supplement_queue = SubagentSupplementQueue(unique_name="")
        try:
            unique_name = SubagentRegistry.register(
                agent_name, supplement_queue, force_unique_name=agent_name,
            )
        except ValueError as e:
            return f"[错误] {e}。请先用 chat-with-{agent_name}(answer=...) 回复当前挂起的子 Agent，或等它结束。"
        supplement_queue.unique_name = unique_name  # 回填唯一名（= agent_name）
        handler._subagent_unique_name = unique_name
        handler._is_sync_subagent = True
        try:
            result_text, return_value = _run_agent_loop(
                client=client,
                system_prompt="",
                system_message=system_message,
                user_input=task,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=20,
                initial_user_content=task,
                context_window_tokens=context_window_tokens,
                context_fifo_threshold=fifo_threshold,
                context_target_threshold=context_target_threshold_val,
                history=history,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
            )
            _maybe_suspend_session(
                unique_name=unique_name,
                return_value=return_value,
                handler=handler,
                client=client,
                tools_schema=tools_schema,
                system_message=system_message,
            )
        finally:
            instance = SubagentRegistry.get(unique_name)
            state = getattr(instance, "state", None) if instance else None
            if state != "waiting_for_answer":
                SubagentRegistry.unregister(unique_name)
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_sync_subagent_interaction.py::test_call_subagent_sync_uses_agent_name_as_unique_name tests/test_sync_subagent_interaction.py::test_call_subagent_sync_second_call_with_answer_resumes_suspended_session tests/test_sync_subagent_interaction.py::test_call_subagent_sync_second_call_same_agent_name_conflict -v 2>&1 | tail -20
```

Expected: 三个测试 PASS。

- [ ] **Step 5: 跑全量同步子 Agent 测试确认无回归**

```bash
python -m pytest tests/test_sync_subagent_interaction.py -v 2>&1 | tail -30
```

Expected: 所有测试 PASS。如有断言随机 hex 后缀的旧测试 FAIL，按 Step 6 修复。

- [ ] **Step 6: 修复同步路径相关测试的随机 hex 断言**

**只改同步路径相关测试**（异步路径测试保持随机 hex 后缀不变）：

```bash
# 列出所有匹配，人工判断哪些是同步路径
grep -rln "browser-operator-[0-9a-f]\{4\}\|file-processor-[0-9a-f]\{4\}\|context-manager-[0-9a-f]\{4\}\|brain-region-[0-9a-f]\{4\}\|test-agent-[0-9a-f]\{4\}\|test-ab\?[0-9]\|test-[0-9a-f]\{4\}" tests/ 2>/dev/null
```

需要改的文件（同步路径相关）：
- `tests/test_sync_subagent_interaction.py`：所有 `browser-operator-xxxx` 改为 `browser-operator`
- `tests/test_at_prefix_interception.py`：**只改同步路径用例** `test_sync_subagent_at_niu_returns_intercepted_sync`（L297/L300/L314）的 `test-ab12` 改为 `test`（同步路径 unique_name=agent_name 格式）。**`test-agent-abc1` 是异步路径用例**（L31/L36/L52/L79/L95/L214 `test_ask_main_agent_impl_returns_terminated_when_cancelled` 和 `test_at_niu_prefix_triggers_ask_main_agent`），保持不变（异步路径仍用 hex 后缀格式）
- `tests/test_call_subagent_with_auto_answer.py`：mock 返回值 `[file-processor-a1b2]` 改为 `[file-processor]`（同步路径格式）

**不改的文件**（异步路径测试，保持随机 hex 后缀）：
- `tests/test_at_message_parser.py`（db_monitor 路由，异步路径）
- `tests/test_db_monitor.py` / `tests/test_db_monitor_ask_routing.py`（异步路径）
- `tests/test_ask_main_agent.py`（异步路径 ask_main_agent 工具）
- `tests/test_subagent_supplement.py`（异步路径 supplement）
- `tests/test_subagent_msg_role.py`（异步路径）
- `tests/test_main_agent_request_queue.py`（异步路径）
- `tests/test_subagents_running_endpoint.py`（异步路径）
- `tests/test_async_subagent_dispatch.py`（异步路径）
- `tests/test_integration_async_complete.py`（异步路径）

每改一个文件跑一遍确认 PASS：

```bash
python -m pytest tests/test_sync_subagent_interaction.py tests/test_at_prefix_interception.py tests/test_call_subagent_with_auto_answer.py -v 2>&1 | tail -30
```

- [ ] **Step 7: Commit**

```bash
git add agent/subagent.py tests/test_sync_subagent_interaction.py tests/test_at_prefix_interception.py tests/test_call_subagent_with_auto_answer.py
git commit -m "$(cat <<'EOF'
feat(subagent): 同步路径用 agent_name 作 unique_name

- call_subagent 同步新任务分支传 force_unique_name=agent_name
- unique_name 为 browser-operator 而非 browser-operator-708b
- 同名冲突时返回错误文本（提示用 chat-with-{agent_name} 回复挂起 session）
- 主 Agent 工具名 chat-with-browser-operator 即 unique_name，LLM 无需记随机后缀
- _ask_main_agent_impl_sync 包装 [browser-operator] 问题（自动跟随 unique_name）
- 修复同步路径相关测试断言（随机 hex → 纯 agent_name），异步路径测试保持不变
- 加 3 个测试：命名稳定性 / 第二次接续 / 同名冲突

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: _extract_unique_name 正则支持纯 agent_name 格式（修 B1）

**Files:**
- Modify: `agent/subagent.py:948-955`
- Test: `tests/test_call_subagent_with_auto_answer.py`

**背景**：v1 计划漏改 `_extract_unique_name`。验证 Agent 确认：`subagent.py:953` 正则 `^\[{agent_name}-[0-9a-f]{4}\] ` 严格匹配 4 位 hex 后缀，方案 B 后同步路径 `[browser-operator] 问题` 不匹配 → 返回 None → `call_subagent_with_auto_answer` 把问题当正常结果返回，子 Agent 挂起 session 残留。程序触发路径（auto_tidy / force 压缩，9 处 compat.py + 1 处 runner.py）全部受影响。

- [ ] **Step 1: 写失败测试——同步路径正则匹配**

在 `tests/test_call_subagent_with_auto_answer.py` 加测试：

```python
def test_extract_unique_name_sync_path_plain_agent_name():
    """同步路径 [browser-operator] 问题 格式能被提取"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("[browser-operator] 第一个问题", "browser-operator") == "browser-operator"
    assert _extract_unique_name("[file-processor] 我该选哪个？", "file-processor") == "file-processor"


def test_extract_unique_name_async_path_hex_suffix_still_works():
    """异步路径 [file-processor-a1b2] 问题 格式仍能被提取（保持向后兼容）"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("[file-processor-a1b2] 第一个问题", "file-processor") == "file-processor-a1b2"
    assert _extract_unique_name("[browser-operator-708b] 问题", "browser-operator") == "browser-operator-708b"


def test_extract_unique_name_no_match_returns_none():
    """非 [子名] 格式返回 None"""
    from agent.subagent import _extract_unique_name
    assert _extract_unique_name("正常结果文本", "browser-operator") is None
    assert _extract_unique_name("[已完成] 任务结束", "browser-operator") is None
    assert _extract_unique_name("[browser-operator-x1yz] 问题", "browser-operator") is None  # x/y/z 非 hex


def test_call_subagent_with_auto_answer_sync_path_auto_replies(monkeypatch):
    """同步路径 [browser-operator] 问题 → call_subagent_with_auto_answer 自动回复"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry

    call_count = {"value": 0}
    call_args_log = []

    def fake_call_subagent(**kwargs):
        call_count["value"] += 1
        call_args_log.append(kwargs.copy())
        # 第一次返回 [browser-operator] 问题（同步路径格式）
        # 第二次返回正常结果
        if call_count["value"] == 1:
            return "[browser-operator] 第一个问题"
        return "子 Agent 完成"

    monkeypatch.setattr(subagent, "call_subagent", fake_call_subagent)

    result = subagent.call_subagent_with_auto_answer(
        agent_name="browser-operator",
        task="测试",
        llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
    )

    # 应自动回复一次，然后第二次返回正常结果
    assert call_count["value"] == 2, f"应调用 2 次，实际：{call_count['value']}"
    assert result == "子 Agent 完成"
    # 第二次调用应传 answer + answer_unique_name
    second_call = call_args_log[1]
    assert second_call.get("answer") is not None
    assert second_call.get("answer_unique_name") == "browser-operator"
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
python -m pytest tests/test_call_subagent_with_auto_answer.py::test_extract_unique_name_sync_path_plain_agent_name tests/test_call_subagent_with_auto_answer.py::test_extract_unique_name_async_path_hex_suffix_still_works tests/test_call_subagent_with_auto_answer.py::test_extract_unique_name_no_match_returns_none tests/test_call_subagent_with_auto_answer.py::test_call_subagent_with_auto_answer_sync_path_auto_replies -v 2>&1 | tail -20
```

Expected: 第一个测试 FAIL（同步路径格式不匹配）；第四个测试 FAIL（自动回复不触发）；第二/三个测试 PASS（原逻辑）。

- [ ] **Step 3: 修改 _extract_unique_name 支持两种格式**

`agent/subagent.py:948-955` 改为：

```python
def _extract_unique_name(result, agent_name):
    """从 '[unique_name] ...' 提取 unique_name，不匹配返回 None。

    支持两种格式（向后兼容）：
    - 同步路径：[agent_name] 问题（如 [browser-operator] 第一个问题）
    - 异步路径：[agent_name-4位hex] 问题（如 [file-processor-a1b2] 第一个问题）

    严格匹配避免误判 `[已完成]` 等正常文本。
    """
    # 优先匹配带 hex 后缀（异步路径）
    pattern_with_hex = rf"^\[({re.escape(agent_name)}-[0-9a-f]{{4}})\] "
    m = re.match(pattern_with_hex, result)
    if m:
        return m.group(1)
    # 再匹配纯 agent_name（同步路径）
    pattern_plain = rf"^\[({re.escape(agent_name)})\] "
    m = re.match(pattern_plain, result)
    return m.group(1) if m else None
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_call_subagent_with_auto_answer.py::test_extract_unique_name_sync_path_plain_agent_name tests/test_call_subagent_with_auto_answer.py::test_extract_unique_name_async_path_hex_suffix_still_works tests/test_call_subagent_with_auto_answer.py::test_extract_unique_name_no_match_returns_none tests/test_call_subagent_with_auto_answer.py::test_call_subagent_with_auto_answer_sync_path_auto_replies -v 2>&1 | tail -20
```

Expected: 四个测试 PASS。

- [ ] **Step 5: 跑全量 call_subagent_with_auto_answer 测试确认无回归**

```bash
python -m pytest tests/test_call_subagent_with_auto_answer.py -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 6: Commit**

```bash
git add agent/subagent.py tests/test_call_subagent_with_auto_answer.py
git commit -m "$(cat <<'EOF'
fix(subagent): _extract_unique_name 支持纯 agent_name 格式

- 原 regex 严格匹配 {agent_name}-[4位hex]，方案 B 后同步路径 [browser-operator] 问题 不匹配
- 改为优先匹配 hex 后缀（异步路径），再匹配纯 agent_name（同步路径）
- 修复程序触发路径（auto_tidy / force 压缩）的 call_subagent_with_auto_answer
- 加 4 个测试：同步路径提取 / 异步路径向后兼容 / 不匹配返回 None / 自动回复端到端

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: _call_subagent_gen fallback + schema + 文档（修 B2/B6）

**Files:**
- Modify: `agent/handler.py:1006-1008`
- Modify: `agent/runner.py:397-401`（schema 描述）
- Modify: `config/agents/niu.md`（文档）
- Test: `tests/test_sync_subagent_interaction.py`

**背景**：验证 Agent 确认 `handler.py:1007` `answer_unique_name=unique_name_arg if answer else None`——LLM 不传 unique_name 时 answer_unique_name=None，call_subagent 第三分支（`if answer is not None and answer_unique_name is not None`）进不去，落到同步新任务分支触发 ValueError 或错误新建空 task session。schema `required: []` 让 LLM 可以省略 unique_name。

- [ ] **Step 1: 写失败测试——LLM 不传 unique_name 时 fallback 到 agent_name**

在 `tests/test_sync_subagent_interaction.py` 加测试：

```python
def test_call_subagent_gen_fallback_unique_name_to_agent_name(monkeypatch):
    """LLM 调 chat-with-browser-operator 传 answer 不传 unique_name → fallback 到 agent_name"""
    from agent import handler
    from agent.handler import NiuHandler

    call_subagent_calls = []

    def fake_call_subagent(**kwargs):
        call_subagent_calls.append(kwargs.copy())
        return "子 Agent 完成"

    # mock handler 必需依赖
    h = NiuHandler.__new__(NiuHandler)
    h.mcp_client = None

    monkeypatch.setattr(handler, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(handler, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(handler, "get_runner", lambda: type("R", (), {"llm_config": {"model": "t", "api_key": "t", "base_url": "h"}})())

    # LLM 调 chat-with-browser-operator 传 answer 不传 unique_name
    gen = h._call_subagent_gen("browser-operator", {
        "task": "",
        "answer": "@browser-operator 我选择 2",
        # 不传 unique_name
    })
    list(gen)  # 消费生成器

    assert len(call_subagent_calls) == 1
    call_kwargs = call_subagent_calls[0]
    # answer_unique_name 应 fallback 到 agent_name
    assert call_kwargs.get("answer_unique_name") == "browser-operator", \
        f"answer_unique_name 应 fallback 到 browser-operator，实际：{call_kwargs.get('answer_unique_name')}"
    assert call_kwargs.get("answer") == "@browser-operator 我选择 2"


def test_call_subagent_gen_explicit_unique_name_overrides_fallback(monkeypatch):
    """LLM 显式传 unique_name 时不用 fallback"""
    from agent import handler
    from agent.handler import NiuHandler

    call_subagent_calls = []
    def fake_call_subagent(**kwargs):
        call_subagent_calls.append(kwargs.copy())
        return "子 Agent 完成"

    h = NiuHandler.__new__(NiuHandler)
    h.mcp_client = None

    monkeypatch.setattr(handler, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(handler, "get_subagent_config", lambda name: {
        "prompt": "test", "temperature": 0.5, "mcpServers": [], "permissions": []
    })
    monkeypatch.setattr(handler, "get_runner", lambda: type("R", (), {"llm_config": {"model": "t", "api_key": "t", "base_url": "h"}})())

    gen = h._call_subagent_gen("browser-operator", {
        "task": "",
        "answer": "@browser-operator 回答",
        "unique_name": "browser-operator",  # 显式传
    })
    list(gen)

    assert call_subagent_calls[0].get("answer_unique_name") == "browser-operator"
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
python -m pytest tests/test_sync_subagent_interaction.py::test_call_subagent_gen_fallback_unique_name_to_agent_name tests/test_sync_subagent_interaction.py::test_call_subagent_gen_explicit_unique_name_overrides_fallback -v 2>&1 | tail -20
```

Expected: 第一个测试 FAIL（answer_unique_name=None，不会 fallback）；第二个测试 PASS（显式传原逻辑）。

- [ ] **Step 3: 修改 _call_subagent_gen 加 fallback**

`agent/handler.py:1000-1008` 改为：

```python
            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
                history=_history,
                answer=answer,
                # 阶段四修复 B2：LLM 不传 unique_name 时 fallback 到 agent_name
                # 同步路径 unique_name=agent_name（方案 B），主 Agent 不需要记随机后缀
                answer_unique_name=(unique_name_arg or agent_name) if answer else None,
            )
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_sync_subagent_interaction.py::test_call_subagent_gen_fallback_unique_name_to_agent_name tests/test_sync_subagent_interaction.py::test_call_subagent_gen_explicit_unique_name_overrides_fallback -v 2>&1 | tail -20
```

Expected: 两个测试 PASS。

- [ ] **Step 5: 修改 schema 描述**

`agent/runner.py:397-401` 改为：

```python
            "unique_name": {
                "type": "string",
                "description": "子 Agent 唯一名。同步调用（chat-with-xxx）时可省略，默认用 agent 名（如 browser-operator）；异步调用时为 agent 名+4位 hex 后缀（如 file-processor-a1b2，来自派单确认）",
            },
```

- [ ] **Step 6: 修改 check_subagent_progress schema 描述**

`agent/runner.py` 的 `check_subagent_progress` 工具 schema（grep 定位 `subagent_name` 描述）改为：

```python
            "subagent_name": {
                "type": "string",
                "description": "子 Agent 唯一名（同步：browser-operator；异步：file-processor-a1b2，来自派单确认或动态注入区）",
            },
```

- [ ] **Step 7: 修改 niu.md 文档**

读 `config/agents/niu.md`，找到关于 `unique_name` 参数的说明（grep `unique_name`），改为：

```markdown
- `unique_name`：子 Agent 唯一名。
  - 同步调用（chat-with-xxx）：可省略，默认用 agent 名（如 browser-operator）。
  - 异步调用：必填，agent 名+4位 hex 后缀（如 file-processor-a1b2，来自派单确认）。
```

并在 niu.md 中"收到同步子 Agent @niu-agent 问题"节后追加：

```markdown
## 收到同步子 Agent 询问后的回复方式

当 chat-with-xxx 工具返回 `[子名] 问题` 格式（如 `[browser-operator] 第一个问题`），说明同步子 Agent 在询问你。回复方式：

**必须用 chat-with-xxx 工具回复**，传 `answer` 参数（不需要 task），`unique_name` 可省略：

\`\`\`
chat-with-browser-operator(
  task="",
  answer="@browser-operator 我选择 2",
)
\`\`\`

**禁止用 content 文本回复**（如 `@browser-operator 我选择 2` 直接输出）——这会导致子 Agent 永久挂起。
```

- [ ] **Step 8: 跑全量 handler 测试确认无回归**

```bash
python -m pytest tests/test_sync_subagent_interaction.py tests/test_call_subagent_with_auto_answer.py -v 2>&1 | tail -30
```

Expected: 所有测试 PASS。

- [ ] **Step 9: Commit**

```bash
git add agent/handler.py agent/runner.py config/agents/niu.md tests/test_sync_subagent_interaction.py
git commit -m "$(cat <<'EOF'
fix(handler): _call_subagent_gen 在 LLM 不传 unique_name 时 fallback 到 agent_name

- 修复 B2/B6：schema required=[] 让 LLM 可省略 unique_name，
  原 handler.py:1007 直接传 None，第三分支进不去，answer 当 task 处理
- 改为 (unique_name_arg or agent_name) if answer else None
- schema 描述更新：unique_name 可省略，默认用 agent_name
- check_subagent_progress schema 描述区分同步/异步格式
- niu.md 文档更新：明确同步子 Agent 询问的回复方式（必须用工具，禁止 content）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 方案 C 最简拦截——复用 FORMAT_ERROR（修 B3/B4/B5）

**Files:**
- Modify: `agent/generic/agent_loop.py:81-84`（改条件让主 Agent 进拦截层）
- Modify: `agent/generic/agent_loop.py:86` 后（加主 Agent 误回复检测，返回 FORMAT_ERROR）
- Test: `tests/test_at_prefix_interception.py`

**背景**：v1 方案 C 依赖 db_monitor → MainAgentRequestQueue → 链路 A → SSE → 前端绕路。验证 Agent 确认：
- B3：`db_monitor.py:194` `_drain_main_agent_request_queue` 检查 `if _chat_lock.locked(): return`，主 Agent chat 期间链路 A 不推送
- B4：`runner.py:2218-2220` finally 块调 `cleanup_suspended_sync_subagents` 清理挂起 session，错误反馈到达时 session 已不存在
- B5：`config/agents/niu.md` 未定义 `[system]` 前缀语义

v3 按用户反馈"程序直接返回错误给主 Agent，它自然进入下一轮"简化：主 Agent content 误回复本质和 MCP 工具调用是同一层——都是程序在处理主 Agent 的输出。`_intercept_at_prefix_content` 已经是"content 意图识别"入口，已有 `FORMAT_ERROR` 常量 + agent_runner_loop 已有 `continue` 重试逻辑（L621-623）。只需改 1 个条件 + 加几行检测，复用现有机制。

- [ ] **Step 1: 写失败测试——主 Agent content 误回复触发 FORMAT_ERROR**

在 `tests/test_at_prefix_interception.py` 加测试：

```python
def test_intercept_main_agent_content_reply_to_sync_suspended_session():
    """主 Agent content @<同步挂起子名> 但无 tool_calls → 返回 FORMAT_ERROR（复用现有常量）"""
    from agent.generic.agent_loop import _intercept_at_prefix_content, FORMAT_ERROR
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    # 注册一个同步挂起 session
    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    instance = SubagentRegistry.get("browser-operator")
    instance.state = "waiting_for_answer"

    # mock handler（主 Agent：memory_context=None + _is_sync_subagent=False）
    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    try:
        status, payload = _intercept_at_prefix_content(
            content="@browser-operator 我选择 2",
            tool_calls=[],  # 主 Agent 没调工具
            messages=messages,
            handler=handler,
            memory_context=None,  # 主 Agent
        )
        assert status == FORMAT_ERROR
        assert payload is None
        # messages 应被追加 assistant content + user 错误提示
        assert len(messages) == 2
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "@browser-operator 我选择 2"
        assert messages[1]["role"] == "user"
        # 错误提示应教 LLM 用 chat-with-xxx 工具回复
        assert "chat-with-browser-operator" in messages[1]["content"]
        assert "answer" in messages[1]["content"]
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_intercept_main_agent_no_suspended_session_no_interception():
    """主 Agent content @子名 但子名不在注册表 → NO_INTERCEPTION（不拦截）"""
    from agent.generic.agent_loop import _intercept_at_prefix_content, NO_INTERCEPTION

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    status, payload = _intercept_at_prefix_content(
        content="@browser-operator 我选择 2",
        tool_calls=[],
        messages=messages,
        handler=handler,
        memory_context=None,
    )
    assert status == NO_INTERCEPTION
    assert payload is None
    assert len(messages) == 0  # 不修改 messages


def test_intercept_main_agent_with_tool_calls_no_interception():
    """主 Agent 调 chat-with-browser-operator 工具 → NO_INTERCEPTION（不拦截，正常工具调用）"""
    from agent.generic.agent_loop import _intercept_at_prefix_content, NO_INTERCEPTION
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    SubagentRegistry.get("browser-operator").state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    try:
        messages = []
        status, payload = _intercept_at_prefix_content(
            content="@browser-operator 我选择 2",
            tool_calls=[{"type": "function", "function": {"name": "chat-with-browser-operator"}}],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == NO_INTERCEPTION
        assert len(messages) == 0
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_intercept_main_agent_async_running_session_no_interception():
    """主 Agent content @<异步 running 子名> → NO_INTERCEPTION（异步路径不拦截，保持 db_monitor 原逻辑）"""
    from agent.generic.agent_loop import _intercept_at_prefix_content, NO_INTERCEPTION
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    name = SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        is_sync=False,  # 异步
    )
    # state 保持默认 "running"（异步路径不调 _maybe_suspend_session）

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    try:
        messages = []
        status, payload = _intercept_at_prefix_content(
            content=f"@{name} 补充上下文",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == NO_INTERCEPTION
        assert len(messages) == 0
    finally:
        SubagentRegistry.unregister(name)


def test_intercept_main_agent_content_with_hex_suffix_old_format():
    """主 Agent content @browser-operator-708b（hex 后缀旧格式）→ 仍能拦截（兼容 LLM 复读历史日志）"""
    from agent.generic.agent_loop import _intercept_at_prefix_content, FORMAT_ERROR
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    # 注册同步挂起 session（unique_name=browser-operator，无 hex 后缀）
    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    SubagentRegistry.get("browser-operator").state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    try:
        # 主 Agent 复读历史日志格式（带 hex 后缀）
        status, _ = _intercept_at_prefix_content(
            content="@browser-operator-708b 我选择 2",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == FORMAT_ERROR  # 应被拦截
        assert len(messages) == 2
        assert "chat-with-browser-operator" in messages[1]["content"]
    finally:
        SubagentRegistry.unregister("browser-operator")


def test_intercept_main_agent_content_with_chinese_punctuation():
    """主 Agent content @browser-operator。我选择 2（无空格中文句号）→ 仍能提取子名并拦截"""
    from agent.generic.agent_loop import _intercept_at_prefix_content, FORMAT_ERROR
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    SubagentRegistry.get("browser-operator").state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    try:
        # content 无空格，直接中文句号
        status, _ = _intercept_at_prefix_content(
            content="@browser-operator。我选择 2",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == FORMAT_ERROR  # 应被拦截
        assert len(messages) == 2
    finally:
        SubagentRegistry.unregister("browser-operator")
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
python -m pytest tests/test_at_prefix_interception.py::test_intercept_main_agent_content_reply_to_sync_suspended_session tests/test_at_prefix_interception.py::test_intercept_main_agent_no_suspended_session_no_interception tests/test_at_prefix_interception.py::test_intercept_main_agent_with_tool_calls_no_interception tests/test_at_prefix_interception.py::test_intercept_main_agent_async_running_session_no_interception tests/test_at_prefix_interception.py::test_intercept_main_agent_content_with_hex_suffix_old_format tests/test_at_prefix_interception.py::test_intercept_main_agent_content_with_chinese_punctuation -v 2>&1 | tail -20
```

Expected: 第一/五/六个测试 FAIL（主 Agent 现在直接 NO_INTERCEPTION，hex 后缀和中文标点场景都不拦截）；其他测试可能 PASS。

- [ ] **Step 3: 修改 _intercept_at_prefix_content 让主 Agent 进拦截层 + 加误回复检测**

`agent/generic/agent_loop.py:81-86` 改为：

```python
    is_sync_subagent = getattr(handler, "_is_sync_subagent", False)
    # tool_calls 时不拦截（正常工具调用）
    if tool_calls:
        return (NO_INTERCEPTION, None)

    stripped = (content or "").lstrip()

    # 主 Agent 分支：检测 content 误回复同步挂起子 Agent
    # 主 Agent 特征：memory_context is None and not is_sync_subagent
    # 误回复模式：content 以 @<同步挂起子名> 开头但本轮没调 chat-with 工具
    if memory_context is None and not is_sync_subagent:
        if _check_main_agent_content_reply_to_suspended(stripped, messages):
            return (FORMAT_ERROR, None)
        return (NO_INTERCEPTION, None)

    # 子 Agent 拦截（原逻辑）：@niu-agent / @end / 格式错误
```

然后在 `_intercept_at_prefix_content` 函数下方加 helper 函数：

```python
def _check_main_agent_content_reply_to_suspended(stripped_content: str, messages: list) -> bool:
    """检测主 Agent content 是否在误回复同步挂起子 Agent。

    误回复模式：content 以 `@<子名> ` 开头，且 <子名> 在 SubagentRegistry 中
    且 state=waiting_for_answer 且 is_sync=True。

    支持两种子名格式（兼容 LLM 复读历史 hex 后缀格式）：
    - 同步路径：browser-operator（方案 B 后默认格式）
    - 异步路径旧格式：browser-operator-708b（LLM 复读 000045 等历史日志格式时出现）

    命中时：append assistant content + user 错误提示，返回 True。
    未命中：返回 False（不拦截）。
    """
    if not stripped_content or not stripped_content.startswith("@"):
        return False

    # 提取 @后的子名（只匹配字母数字下划线连字符，遇到中文标点等非 ASCII 立即停止）
    # 避免 @browser-operator。我选择 提取到 browser-operator。我选择 的错误
    m = re.match(r"@([A-Za-z0-9_\-]+)", stripped_content)
    if not m:
        return False
    target = m.group(1)

    # strip 非字母数字尾部（处理 LLM 误加英文标点）
    target_clean = target.rstrip(".,!?;:")

    from agent.subagent_registry import SubagentRegistry
    instance = SubagentRegistry.get(target_clean)

    # 兜底：target 含 hex 后缀旧格式（如 browser-operator-708b）时，提取 agent_type 再查
    # 兼容 LLM 复读历史日志格式的场景（000045 真实日志主 Agent 误回复就是 hex 后缀格式）
    if instance is None:
        hex_match = re.match(r"^(.+)-[0-9a-f]{4}$", target_clean)
        if hex_match:
            agent_type_candidate = hex_match.group(1)
            instance = SubagentRegistry.get(agent_type_candidate)
            if instance is not None:
                target_clean = agent_type_candidate  # 用真实 unique_name 更新

    if instance is None:
        return False  # 不在注册表，不拦截

    # 只拦截同步挂起 session（异步 running 走 db_monitor 原逻辑）
    if getattr(instance, "state", "running") != "waiting_for_answer":
        return False
    if not getattr(instance, "is_sync", True):
        return False

    # 命中误回复模式：append 错误提示，返回 FORMAT_ERROR
    agent_type = instance.agent_type
    error_prompt = (
        f"[对话格式错误] 你刚才用 content 文本回复了同步子 Agent {target_clean}，"
        f"这会导致它永久挂起。同步子 Agent 询问必须用工具回复。\n\n"
        f"请立即调用 chat-with-{agent_type} 工具，参数：\n"
        f"- task: \"\"（空字符串）\n"
        f"- answer: 你刚才想回复的内容（如 \"@{agent_type} 我选择 2\"）\n"
        f"- unique_name: 可省略（默认用 {agent_type}）\n\n"
        f"禁止再用 content 文本回复。"
    )
    messages.append({"role": "assistant", "content": stripped_content})  # 用 stripped 与子 Agent 分支（用 content）略不一致，但主 Agent 误回复 content 通常无前导空格，无实际影响
    messages.append({"role": "user", "content": error_prompt})
    logger.info(f"[AtPrefix] 主 Agent content 误回复同步挂起子 Agent {target_clean}，注入 FORMAT_ERROR 提示")
    return True
```

**关键设计点**：
- helper 用 `re.match(r"@([A-Za-z0-9_\-]+)", ...)` 严格提取子名，遇到中文标点立即停止（避免吞标点后的内容）
- 加 hex 后缀兜底：`target` 含 `-708b` 等 4 位 hex 后缀时，提取 agent_type 再查 registry（兼容 LLM 复读历史日志格式的场景，000045 真实日志正是这种格式）
- 只拦截同步 `waiting_for_answer` session，异步 running 不拦截（保持 db_monitor 原逻辑）

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_at_prefix_interception.py::test_intercept_main_agent_content_reply_to_sync_suspended_session tests/test_at_prefix_interception.py::test_intercept_main_agent_no_suspended_session_no_interception tests/test_at_prefix_interception.py::test_intercept_main_agent_with_tool_calls_no_interception tests/test_at_prefix_interception.py::test_intercept_main_agent_async_running_session_no_interception tests/test_at_prefix_interception.py::test_intercept_main_agent_content_with_hex_suffix_old_format tests/test_at_prefix_interception.py::test_intercept_main_agent_content_with_chinese_punctuation -v 2>&1 | tail -20
```

Expected: 六个测试 PASS。

- [ ] **Step 5: 跑全量拦截层测试确认无回归**

```bash
python -m pytest tests/test_at_prefix_interception.py -v 2>&1 | tail -30
```

Expected: 所有测试 PASS。

- [ ] **Step 6: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_at_prefix_interception.py
git commit -m "$(cat <<'EOF'
feat(agent_loop): 主 Agent content 误回复同步挂起子 Agent 时返回 FORMAT_ERROR

v3 最简方案——复用现有 FORMAT_ERROR 机制：
- 改 _intercept_at_prefix_content L83 条件：让主 Agent 也进拦截层（之前直接 NO_INTERCEPTION）
- 加 _check_main_agent_content_reply_to_suspended helper：检测 content 以 @<同步挂起子名> 开头
- 命中时 append assistant content + user 错误提示（教 LLM 用 chat-with-xxx 工具回复）
- 返回 FORMAT_ERROR，agent_runner_loop 复用现有 continue 逻辑（L621-623）让 LLM 重做
- 不加新常量、不加 agent_runner_loop 改动

修复 v1 方案 C 的 3 个 BLOCKER：
- B3：db_monitor 链路 A 在 _chat_lock 持有期间不推送 → 现在不需要 db_monitor，工具循环内直接处理
- B4：cleanup_suspended_sync_subagents 在 finally 清理挂起 session → 现在工具循环内就反馈，不依赖 cleanup 时序
- B5：niu.md 未定义 [system] 前缀语义 → 现在用 [对话格式错误] 前缀，主 Agent 已熟悉（与子 Agent FORMAT_ERROR 同源）

只拦截同步 waiting_for_answer session，异步 running 走 db_monitor 原逻辑（不拦截）。
helper 兼容两种场景：
- LLM 复读历史 hex 后缀格式（@browser-operator-708b）→ 提取 agent_type 再查 registry
- content 无空格含中文标点（@browser-operator。我选择 2）→ 严格正则只匹配字母数字+连字符
加 6 个测试：误回复触发 / 无挂起 session 不拦截 / 有 tool_calls 不拦截 / 异步 running 不拦截 / hex 后缀旧格式 / 中文标点无空格

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: 端到端 mock 测试——主 Agent 误用 content 触发拦截

**Files:**
- Test: `tests/test_sync_subagent_interaction.py`

- [ ] **Step 1: 写端到端测试——主 Agent content 误回复被拦截层捕获**

在 `tests/test_sync_subagent_interaction.py` 末尾加：

```python
def test_e2e_main_agent_content_reply_intercepted_before_yield(monkeypatch):
    """端到端：主 Agent content 误回复同步挂起子 Agent 时，拦截层在 yield 前捕获，LLM 重做。

    模拟主 Agent 下一轮输出 content @browser-operator 回答 但无 tool_calls：
    - 拦截层应返回 FORMAT_ERROR（v3 复用现有常量）
    - agent_runner_loop 应 continue（不 yield content 给前端）
    - messages 应含 assistant content + user 错误提示
    - LLM 下一轮应看到错误提示并改用工具回复
    """
    from agent.generic.agent_loop import _intercept_at_prefix_content, FORMAT_ERROR
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    # 注册同步挂起 session
    sq = SubagentSupplementQueue(unique_name="")
    SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    SubagentRegistry.get("browser-operator").state = "waiting_for_answer"

    class FakeHandler:
        _is_sync_subagent = False
        _subagent_unique_name = ""
    handler = FakeHandler()

    messages = []
    try:
        # 第一轮：主 Agent 误用 content 回复
        status, _ = _intercept_at_prefix_content(
            content="@browser-operator 我选择 2",
            tool_calls=[],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == FORMAT_ERROR
        assert len(messages) == 2
        assert "chat-with-browser-operator" in messages[1]["content"]

        # 模拟 LLM 下一轮看到错误提示后改用工具
        # tool_calls 非空时拦截层应返回 NO_INTERCEPTION（正常工具调用）
        messages.clear()
        from agent.generic.agent_loop import NO_INTERCEPTION
        status, _ = _intercept_at_prefix_content(
            content="",  # 调工具时 content 通常为空
            tool_calls=[{"type": "function", "function": {"name": "chat-with-browser-operator"}}],
            messages=messages,
            handler=handler,
            memory_context=None,
        )
        assert status == NO_INTERCEPTION
        assert len(messages) == 0  # 工具调用不拦截
    finally:
        SubagentRegistry.unregister("browser-operator")
```

- [ ] **Step 2: 跑测试确认 PASS**

```bash
python -m pytest tests/test_sync_subagent_interaction.py::test_e2e_main_agent_content_reply_intercepted_before_yield -v 2>&1 | tail -20
```

Expected: PASS（依赖 Task 5 的拦截层已生效）。

- [ ] **Step 3: Commit**

```bash
git add tests/test_sync_subagent_interaction.py
git commit -m "$(cat <<'EOF'
test(sync_subagent): 端到端验证 content 误回复被拦截层捕获

- 模拟主 Agent content @browser-operator 回复同步挂起 session
- 验证拦截层返回 FORMAT_ERROR（不 yield 给前端）
- 验证 messages 含错误提示（教 LLM 用 chat-with-xxx 工具）
- 验证 LLM 下一轮调工具时不再被拦截（NO_INTERCEPTION）
- 真实 LLM 端到端验证由 Task 7 完成

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: 全量测试 + 真实端到端验证 + 更新记忆

**Files:**
- 无代码改动，仅验证

- [ ] **Step 1: 跑全量测试套件确认无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/ -v 2>&1 | tail -40
```

Expected: 所有测试 PASS。如有 FAIL，逐个排查。

- [ ] **Step 2: 真实端到端验证——启动程序**

```bash
./niu &
sleep 5
ps aux | grep -E "niu|python" | grep -v grep | head -5
```

确认程序启动正常，无 import 错误。

- [ ] **Step 3: 真实端到端验证——主 Agent 委托同步子 Agent 触发 @niu-agent 询问**

在前端或用 curl 调 `/api/chat/session`，发消息让主 Agent 委托 `file-processor` 子 Agent 完成任务（如"处理这个文档，遇到分类选项问我"）。

注：`config/agents/` 下现有同步子 Agent 有 `file-processor.md`/`event-manager.md`/`journal-agent.md`/`context-manager.md`/`entity-extractor.md`/`dream-evolver.md`，没有 `browser-operator.md`。计划全文以 `browser-operator` 为示例（因为它对应 000045 真实日志场景），但真实 LLM 验证时需用现有子 Agent。下面步骤以 `file-processor` 为例，可替换为任一现有同步子 Agent。

观察日志：

```bash
tail -f logs/raw_http/$(date +%Y%m%d)/*.json | grep -E "@niu-agent|file-processor|chat-with|FORMAT_ERROR|主 Agent content 误回复"
```

Expected 子 Agent 阶段：
- 子 Agent 输出 `@niu-agent 第一个问题是：...`
- 拦截层包装成 `[file-processor] 第一个问题是：...`（**不再是 `[file-processor-a1b2]`**）
- call_subagent 返回 JSON `{"status":"success","result":"[file-processor] ..."}` 给主 Agent

- [ ] **Step 4: 真实端到端验证——主 Agent 正确调 chat-with-file-processor 工具回复**

观察主 Agent 下一步：应调用 `chat-with-file-processor(answer="@file-processor 我选择 2", task="")` 工具。

Expected：
- call_subagent 第三分支从 registry 拿回挂起 session
- 注入 `[主 Agent 回答] 我选择 2` 到 resumed_messages
- 子 Agent 继续跑工具循环
- 子 Agent 输出 `@end 任务完成` 或继续 @niu-agent

- [ ] **Step 5: 真实端到端验证——主 Agent 误用 content 回复时被拦截层捕获**

如果主 Agent LLM 仍误用 content `@file-processor 回答`（可能发生）：

Expected agent_loop 日志：
```
[AtPrefix] 主 Agent content 误回复同步挂起子 Agent file-processor，注入 FORMAT_ERROR 提示
```

主 Agent 下一轮应看到错误提示，改为调 chat-with-file-processor 工具。

观察主 Agent 是否在 `yield StreamEvent("reply", content)` 之前被拦截（不应 yield 给前端）。

- [ ] **Step 6: 测试结束清理**

```bash
kill -TERM $(pgrep -f "niu|python.*niu_api" | head -5)
sleep 3
ps aux | grep -E "niu|python.*niu" | grep -v grep
```

确认所有进程已退出（无僵尸进程）。

- [ ] **Step 7: 更新记忆文件**

更新 `REDACTED_USER_PATH/.claude/projects/-Users-lilei-tools-ai-bot/memory/stage4-sync-subagent-interaction.md`：
- 标注"同步 @niu-agent 询问路径失败已修复（方案 B + 方案 C v2 重构）"
- 更新实施进度：加本次修复的 commit hash
- 移除"待修复问题"段，改为"已修复"
- 记录 v2 重构原因（v1 方案 C 时序断裂：B3/B4/B5）

- [ ] **Step 8: 最终总结**

无需 git commit（记忆文件在 ~/.claude/ 下，不在仓库内）。

---

## Self-Review 检查

**1. Spec 覆盖**：
- 方案 B 命名修复 → Task 1（register）+ Task 2（call_subagent）✅
- 方案 B 连带 BLOCKER → Task 3（_extract_unique_name 修 B1）+ Task 4（_call_subagent_gen 修 B2/B6）✅
- 方案 C 最简拦截 → Task 5（主 Agent 拦截层修 B3/B4/B5，复用 FORMAT_ERROR）✅
- 端到端验证 → Task 6（mock）+ Task 7（真实 LLM）✅

**2. 类型一致性**：
- `force_unique_name: Optional[str] = None` — Task 1 定义，Task 2 调用 ✅
- `FORMAT_ERROR` 复用现有常量 — Task 5 不新增常量，agent_runner_loop 已有 continue 逻辑（L621-623）✅
- `_check_main_agent_content_reply_to_suspended` 返回 `bool`，True 表示命中（已 append 错误提示），False 表示未命中 ✅
- `instance.state == "waiting_for_answer"` — Task 5 检测，与 RunningSubagent.state 字段一致 ✅
- `chat-with-{agent_type}` — Task 5 错误提示用 instance.agent_type ✅

**3. 占位符扫描**：无 TBD/TODO，所有步骤有具体代码 ✅

**4. 关键执行路径分析**：
- Task 5 主 Agent 走 NO_INTERCEPTION fallthrough 仍会执行 L627-635 validate_references + L638 yield reply——这是期望行为（正常主 Agent 回复不被拦截）✅
- Task 5 主 Agent 误回复命中时返回 FORMAT_ERROR，L621-623 `continue` 直接回 while 循环顶部，不执行 L638 yield reply——主 Agent 不会 yield content 给前端 ✅
- Task 5 helper 兼容 LLM 复读历史 hex 后缀格式（`@browser-operator-708b`）——提取 agent_type 再查 registry，000045 真实日志场景被覆盖 ✅
- Task 5 helper 严格正则 `[A-Za-z0-9_\-]+` 提取子名——避免吞中文标点后的内容 ✅

**5. 风险点与限制**：
- Task 5 拦截层只检测 content 以 `@` 开头的场景——如果主 Agent 写 `好的，@browser-operator 我选择 2`（content 不以 @ 开头），拦截层不命中。这是已知限制，但 000045 真实日志显示主 Agent 误回复时确实以 `@` 开头（模仿异步路径写法），这个限制可接受
- Task 7 Step 5 真实 LLM 测试——LLM 行为不固定，主 Agent 可能一次就调对工具（不触发 Task 5 拦截）。Step 5 用"如果仍误用"措辞，方案 C 拦截路径靠 Task 6 mock 测试覆盖
- Task 5 重构后，db_monitor L157 的降级逻辑仍保留（异步路径需要）——但同步挂起 session 不会再走 db_monitor 路径（主 Agent 拦截层先捕获），所以 L157 的同步降级分支实际不再触发。如果未来有其他路径绕过拦截层，db_monitor L157 仍是静默降级——这是已知遗留问题，但不在本次修复范围
- Task 2 Step 3 同步路径 `ValueError` 捕获后返回 `[错误] ...` 文本，`_call_subagent_gen` 会把它当 `status: success` 的结果返回——主 Agent 可能误以为子 Agent 真的报错了。错误文本已含明确指引（`请先用 chat-with-{agent_name}(answer=...) 回复`），主 Agent 应能识别。如果未来发现 LLM 仍误判，可在 `_call_subagent_gen` 检测 result 以 `[错误]` 开头时改用 `status: error`——这是未来优化点，不在本次修复范围
- `tests/test_at_prefix_interception.py` 现有 3 个主 Agent 测试（L170/L264/L317）用 `mock.MagicMock()` + 显式 `_is_sync_subagent=False`——v3 改 L83 后这些测试仍 PASS（content 不以 @ 开头，helper 返回 False，NO_INTERCEPTION）。但 MagicMock 的 truthy 属性是隐患：未来如果有人加测试不显式设 `_is_sync_subagent=False`，会走错分支。Task 5 Step 5 跑全量测试时应关注这点

