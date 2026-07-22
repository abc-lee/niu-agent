# context-manager 模式一走标准 @end 结束逻辑整改 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复压缩子 Agent 模式一"空响应被误判为压缩完成、提前退出且游标误推进"的问题：拦截层绕过条件从"按 agent 名字硬编码"改为"按 handler 显式开关"，模式一走标准 @end/FORMAT_ERROR 结束判断，模式二/三显式传开关保持一轮出方案行为不变。

**Architecture:** `call_subagent` 新增 `bypass_at_prefix: bool = False` 参数，透传到 `handler._bypass_at_prefix`；`agent_loop.py` 拦截层按该开关 `is True` 严格判断是否绕过（`is True` 防 MagicMock truthy 污染）；模式二/三共 3 个调用点显式传 `True`，模式一不传（默认 `False`）；`context-manager.md` 模式一章节补充 @end 结束方式说明。

**Tech Stack:** Python 3.11（项目自带 `python/bin/python`）、pytest + monkeypatch/MagicMock 单元测试、ruff

---

## 背景（事故链）

2026-07-22 11:46（`logs/raw_http/20260722/000043_*.json`），模式一压缩第 7 轮，豆包 ark-code-latest 把 `delete_messages` 工具调用 XML 泄漏进 thinking 字段，正式响应为 `content: ""` + `tool_calls: []`。

`agent/generic/agent_loop.py:86-91` 拦截层对 `unique_name == "context-manager"` **按名字无条件绕过**（不区分模式）→ 空响应落到通用退出分支（L1052-1066）返回 `CURRENT_TASK_DONE` → 压缩循环提前退出。更严重的是 `niu_api/compat.py:2948-2955` 游标照常推进，未处理的消息被标记为"已压缩"，后续增量压缩永久跳过。

该绕过是 2026-07-08 为修模式三 `keep=` 输出被 FORMAT_ERROR 误杀而加的（`docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md`），当时只考虑一轮出方案场景，模式一被误伤套进同一绕过。

**整改原则**：模式二/三一旦不是一轮输出，追问引发的第二轮会把全量消息再发一遍造成上下文溢出——所以模式二/三必须保持"一轮即结束"行为逐字节不变；模式一是多轮工具交互，走标准子 Agent 结束逻辑（无 @end 且无 tool_calls → FORMAT_ERROR 追问 → 继续循环）。

## 影响面审计（已完成，无需重复）

`call_subagent` / `call_subagent_with_auto_answer` 全仓库调用点审计结论：

- 生产代码 13 处调用点。其中 `agent_name="context-manager"` 仅 3 处：`niu_api/compat.py:2704`（模式二）、`niu_api/compat.py:2927`（模式一）、`niu_api/compat.py:3375`（模式三）；另有 `agent/runner.py:1330`（模式三）也调 context-manager。其余 6 处 compat.py 调用点（L2364/2445/2527/3101/3180/3262）均为 entity-extractor / dream-evolver / journal-agent，**本来就不被名字绕过，本次行为不变**。
- `call_subagent_with_auto_answer`（`agent/subagent.py:955-983`）用 `**kwargs` 透传全部参数（首调 L972、回覆调 L977-983），新增参数自动透传，**helper 签名不用改**。
- 测试代码 21 处调用点全部 `*args, **kwargs` 或 keyword 传参，新增带默认值参数完全兼容。
- `tests/test_at_prefix_interception.py` 20 个测试、`tests/test_context_manager_bypass_at_prefix.py` L84 对照组测试，handler 均为 `mock.MagicMock()`：拦截层用 `is True` 严格判断后，MagicMock 的 `_bypass_at_prefix` 属性（mock 对象）`is True` 为 `False` → 不绕过 → **这些测试零改动通过**。

## 实施约束（implementer 必读）

- 禁止使用 git worktree；禁止访问外网
- 修改代码前若工作区有未提交改动，先 `git add -A && git commit` 备份；工作区干净则不用空提交
- Python 解释器一律用 `python/bin/python`（项目自带 venv）
- 每个 Task 完成后跑 `python/bin/python -m ruff check agent/ niu_api/`（如无 ruff 可执行则跳过）
- **Task 2 与 Task 3 必须同批交付**：只上 Task 2 时模式一正常完成会先吃一轮 FORMAT_ERROR 追问才退出（能自愈但多烧一轮 token）；只上 Task 3 无效果

---

### Task 1: `call_subagent` 增加 `bypass_at_prefix` 参数

纯加参数（默认 `False`），不改拦截层——本 Task 完成后运行时行为与现状完全一致。

**Files:**
- Modify: `agent/subagent.py:705-718`（签名）、`agent/subagent.py:781`（赋值）
- Modify: `tests/test_subagent_overflow.py:64,387,419`（修复既有 mock 缺陷，见 Step 6）
- Test: `tests/test_context_manager_bypass_at_prefix.py`

- [ ] **Step 1: 写 2 个失败测试（参数透传）**

在 `tests/test_context_manager_bypass_at_prefix.py` 文件末尾追加：

```python
def test_call_subagent_passes_bypass_at_prefix_to_handler(monkeypatch):
    """call_subagent(bypass_at_prefix=True) 时，内部 handler._bypass_at_prefix 为 True"""
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema,
                 max_turns=20, initial_user_content=None, context_window_tokens=0,
                 context_fifo_threshold=0, history=None, **kwargs):
        captured["handler"] = handler
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

    import agent.runner as runner_mod
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

    subagent.call_subagent(
        agent_name="test-agent",
        task="t",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        bypass_at_prefix=True,
    )
    assert captured["handler"]._bypass_at_prefix is True


def test_call_subagent_default_bypass_at_prefix_false(monkeypatch):
    """不传 bypass_at_prefix 时，handler._bypass_at_prefix 为 False（走标准 @end 拦截）"""
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema,
                 max_turns=20, initial_user_content=None, context_window_tokens=0,
                 context_fifo_threshold=0, history=None, **kwargs):
        captured["handler"] = handler
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

    import agent.runner as runner_mod
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

    subagent.call_subagent(
        agent_name="test-agent",
        task="t",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert captured["handler"]._bypass_at_prefix is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python/bin/python -m pytest tests/test_context_manager_bypass_at_prefix.py -v`
Expected:
- `test_call_subagent_passes_bypass_at_prefix_to_handler` FAIL——`TypeError: call_subagent() got an unexpected keyword argument 'bypass_at_prefix'`（签名未加，函数入口处即炸）
- `test_call_subagent_default_bypass_at_prefix_false` FAIL——`AttributeError: 'NiuHandler' object has no attribute '_bypass_at_prefix'`（函数体跑通，断言处属性不存在）
- 现有 5 个测试 PASS

- [ ] **Step 3: 修改 `agent/subagent.py` 签名**

`agent/subagent.py:705-717`，把签名最后一行：

```python
    answer_unique_name: Optional[str] = None,  # 阶段四新增：回复路径锁定挂起 session
) -> str:
```

改为：

```python
    answer_unique_name: Optional[str] = None,  # 阶段四新增：回复路径锁定挂起 session
    bypass_at_prefix: bool = False,  # True=绕过@前缀拦截层（仅一轮出方案的子Agent用，如context-manager模式二/三）
) -> str:
```

- [ ] **Step 4: 修改 `agent/subagent.py` 赋值**

`agent/subagent.py:781`，把：

```python
    handler._is_subagent = True
```

改为：

```python
    handler._is_subagent = True
    # @前缀拦截层绕过开关：仅一轮出方案的子 Agent（context-manager 模式二/三）由调用方
    # 显式传 bypass_at_prefix=True 开启；模式一（多轮工具）保持默认 False，走标准 @end/FORMAT_ERROR 结束判断
    handler._bypass_at_prefix = bypass_at_prefix
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python/bin/python -m pytest tests/test_context_manager_bypass_at_prefix.py -v`
Expected: 7 passed（现有 5 + 新增 2）

- [ ] **Step 6: 修复 test_subagent_overflow.py 既有 mock 缺陷**

背景：`agent/subagent.py:672` 实际调用是 `get_tools_schema(include_main_only=False)`，而 `tests/test_subagent_overflow.py` 的 3 处 mock 是 `lambda: []`（不接受该 kwarg）→ 该文件 3 个测试在 main 上已 FAIL（同款 TypeError，计划审查阶段实测确认）。本 Step 顺带修复，否则 Step 7 回归预期"全部 PASS"不成立。

`tests/test_subagent_overflow.py` 共 3 处（L64、L387、L419），逐字相同，用 Edit replace_all：

```python
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda: [])
```

改为：

```python
        monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
```

验证：`python/bin/python -m pytest tests/test_subagent_overflow.py -q`
Expected: 32 passed（原 29 passed + 修复 3 failed）

- [ ] **Step 7: 回归确认 + 代码检查**

Run: `python/bin/python -m pytest tests/test_call_subagent_with_auto_answer.py tests/test_subagent_overflow.py tests/test_at_prefix_interception.py -v`
Expected: 全部 PASS（本 Task 行为零变化 + Step 6 修复的 3 个既有失败转绿）

Run: `python/bin/python -m ruff check agent/subagent.py`
Expected: 无新增告警

- [ ] **Step 8: Commit**

```bash
git add agent/subagent.py tests/test_context_manager_bypass_at_prefix.py tests/test_subagent_overflow.py
git commit -m "feat(agent): call_subagent 新增 bypass_at_prefix 参数（默认 False，行为不变）；顺带修复 test_subagent_overflow 3 处 get_tools_schema mock 既有缺陷"
```

---

### Task 2: 拦截层改开关 + 模式二/三调用点标注（原子变更）

本 Task 必须原子完成：拦截层改为按开关判断后，模式二/三若未同时标注 `bypass_at_prefix=True`，会被 FORMAT_ERROR 追问进入第二轮，全量消息重发造成上下文溢出。

**Files:**
- Modify: `agent/generic/agent_loop.py:86-91`（拦截层绕过条件）
- Modify: `niu_api/compat.py:2703-2711`（模式二调用点）
- Modify: `niu_api/compat.py:3374-3382`（模式三调用点）
- Modify: `agent/runner.py:1329-1337`（模式三调用点）
- Test: `tests/test_context_manager_bypass_at_prefix.py`

**注意**：`niu_api/compat.py:2926-2933`（模式一调用点 `run_context_manager`）**不改**——不传参默认 `False`，走标准结束逻辑，这正是本次整改目标。

- [ ] **Step 1: 更新测试文件 docstring + 2 个现有测试 + 新增 1 个模式一测试**

`tests/test_context_manager_bypass_at_prefix.py` 文件头 docstring（L1-9）整体替换为：

```python
"""context-manager @前缀拦截层绕过开关的单元测试。

背景：拦截层曾按 agent 名字（unique_name == "context-manager"）无条件绕过，误伤模式一
（多轮工具交互）：2026-07-22 模式一压缩中 LLM 空响应被误判为压缩完成，提前退出且游标误推进。
整改后绕过由调用方显式传 call_subagent(bypass_at_prefix=True) 开启：
- 模式二/三（一轮出 keep=/update=/cursor= 方案）：传 True 绕过，行为与整改前一致
- 模式一（多轮工具）：默认 False，走标准 @end/FORMAT_ERROR 结束判断
本测试验证：
1. context-manager 系统提示词不含 @niu-agent 守则（保持不变）
2. bypass_at_prefix=True 时输出 keep= 方案不被拦截（模式二/三行为锁定）
3. bypass_at_prefix=False 时空响应走 FORMAT_ERROR 追问（模式一新行为）
4. 其他子 Agent（file-processor）仍被注入守则、仍被拦截（不受影响）
5. call_subagent 把 bypass_at_prefix 参数透传到 handler._bypass_at_prefix
"""
```

`test_context_manager_keep_output_not_intercepted`（L34-56）整体替换为：

```python
def test_context_manager_keep_output_not_intercepted():
    """模式二/三（bypass_at_prefix=True）：输出 keep=/update=/cursor= 时，拦截层返回 NO_INTERCEPTION"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True  # 同步路径
    fake_handler._bypass_at_prefix = True  # 一轮出方案显式绕过（模式二/三路径）
    messages = [
        {"role": "system", "content": "你是 context-manager"},
        {"role": "user", "content": "压缩这些消息"},
    ]
    content = "<analysis>分析...</analysis>\nkeep=1,2,3\nupdate=4|[摘要] xxx\ncursor=3"

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 同步调用，memory_context=None
    )
    assert result == (agent_loop.NO_INTERCEPTION, None)
    # 验证 messages 没有被追加格式错误提示
    assert len(messages) == 2  # 原始两条不动
```

`test_context_manager_bypass_doesnt_append_format_error`（L59-81）整体替换为：

```python
def test_context_manager_bypass_doesnt_append_format_error():
    """模式二/三（bypass_at_prefix=True）：输出无 @ 前缀时，messages 不被追加 [对话格式错误] 提示"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True
    fake_handler._bypass_at_prefix = True  # 一轮出方案显式绕过（模式二/三路径）
    messages = [
        {"role": "system", "content": "你是 context-manager"},
        {"role": "user", "content": "压缩"},
    ]
    original_len = len(messages)
    content = "keep=1,5,10\nupdate=2|[摘要] xxx\ncursor=10"

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )
    assert result == (agent_loop.NO_INTERCEPTION, None)
    assert len(messages) == original_len  # 关键：messages 不变，没有追加 FORMAT_ERROR 提示
```

在 `test_file_processor_still_intercepted_when_no_at_prefix`（L84-104，**保持不动**）之后、Task 1 新增的 2 个参数透传测试之前，插入新模式一测试：

```python
def test_context_manager_mode1_no_bypass_goes_format_error():
    """模式一（_bypass_at_prefix=False）：空响应走标准 FORMAT_ERROR 追问，不再按名字绕过。

    回归 2026-07-22 事故：模式一压缩第 7 轮 LLM 把 delete_messages 泄漏进 thinking
    （正式响应 content="" + tool_calls=[]），按名字绕过使程序误判压缩完成、游标误推进。
    """
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True
    fake_handler._bypass_at_prefix = False  # 模式一：默认 False，走标准结束判断
    messages = [
        {"role": "system", "content": "你是 context-manager"},
        {"role": "user", "content": "压缩这些消息"},
    ]
    content = ""  # 空响应（事故触发场景：工具调用泄漏进 thinking 后的正式响应）

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )
    assert result == (agent_loop.FORMAT_ERROR, None)
    # 验证 messages 被追加 assistant 空响应 + FORMAT_ERROR user 追问
    assert len(messages) == 4
    assert messages[-2] == {"role": "assistant", "content": ""}
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]
```

- [ ] **Step 2: 跑测试确认新测试失败、其余通过**

Run: `python/bin/python -m pytest tests/test_context_manager_bypass_at_prefix.py -v`
Expected:
- `test_context_manager_mode1_no_bypass_goes_format_error` FAIL——拦截层仍按名字绕过，返回 `(NO_INTERCEPTION, None)` 而非 `(FORMAT_ERROR, None)`
- 其余 7 个测试 PASS（2 个更新的测试仍被名字绕过 → 行为不变 → 通过）

- [ ] **Step 3: 修改拦截层绕过条件**

`agent/generic/agent_loop.py:86-91`，把：

```python
    # context-manager 绕过：它原设计是直接输出 keep=/update=/cursor= 让程序写数据库，
    # 不走 @niu-agent/@end 交互通道。拦截会导致正确输出被 FORMAT_ERROR，压缩失败。
    # 详见 docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
    unique_name = getattr(handler, "_subagent_unique_name", "") or ""
    if unique_name == "context-manager":
        return (NO_INTERCEPTION, None)
```

改为：

```python
    # 一轮出方案的子 Agent（context-manager 模式二/三）绕过@前缀拦截：
    # 它们直接输出 keep=/update=/cursor= 让程序写数据库，拦截会导致正确输出被 FORMAT_ERROR，
    # 且追问引发的第二轮会把全量消息再发一遍，造成上下文溢出。
    # 由调用方经 call_subagent(bypass_at_prefix=True) 显式开启；模式一（多轮工具）不开启，
    # 走标准 @end/FORMAT_ERROR 结束判断。
    # 详见 docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
    # 必须 is True 严格判断：测试常用 MagicMock handler，其同名属性是 truthy mock 对象，
    # 宽松判断会把所有 mock handler 误判为绕过，令 test_at_prefix_interception.py 大批失败。
    if getattr(handler, "_bypass_at_prefix", False) is True:
        return (NO_INTERCEPTION, None)
```

（即删除 L89 的 `unique_name = ...` 赋值行和 L90-91 的名字比对——L113 的 @niu-agent 分支自己重新 `getattr` 取 unique_name，不受影响。）

- [ ] **Step 4: 标注模式二调用点**

`niu_api/compat.py:2703-2711`，把：

```python
                    def run_context_manager_mode2():
                        return call_subagent_with_auto_answer(
                            agent_name="context-manager",
                            task=prompt,
                            llm_config=llm_config_with_max,
                            mcp_client=None,
                            context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
                            history=compress_history,  # 直接传 messages 列表，避免单条 user message 超限
                        )
```

改为：

```python
                    def run_context_manager_mode2():
                        return call_subagent_with_auto_answer(
                            agent_name="context-manager",
                            task=prompt,
                            llm_config=llm_config_with_max,
                            mcp_client=None,
                            context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
                            history=compress_history,  # 直接传 messages 列表，避免单条 user message 超限
                            bypass_at_prefix=True,  # 一轮出方案：绕过@前缀拦截，禁止追问第二轮（防上下文溢出）
                        )
```

- [ ] **Step 5: 标注模式三调用点（compat.py）**

`niu_api/compat.py:3374-3382`，把：

```python
            def run_context_manager_force():
                return call_subagent_with_auto_answer(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config_with_max,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=_force_history,  # 直接传 messages 列表，避免单条 user message 超限
                )
```

改为：

```python
            def run_context_manager_force():
                return call_subagent_with_auto_answer(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config_with_max,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=_force_history,  # 直接传 messages 列表，避免单条 user message 超限
                    bypass_at_prefix=True,  # 一轮出方案：绕过@前缀拦截，禁止追问第二轮（防上下文溢出）
                )
```

- [ ] **Step 6: 标注模式三调用点（runner.py）**

`agent/runner.py:1329-1337`，把：

```python
            def run_context_manager_force():
                return call_subagent_with_auto_answer(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config_with_max,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=_force_history,
                )
```

改为：

```python
            def run_context_manager_force():
                return call_subagent_with_auto_answer(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config_with_max,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=_force_history,
                    bypass_at_prefix=True,  # 一轮出方案：绕过@前缀拦截，禁止追问第二轮（防上下文溢出）
                )
```

- [ ] **Step 7: 跑专项测试确认全部通过**

Run: `python/bin/python -m pytest tests/test_context_manager_bypass_at_prefix.py tests/test_at_prefix_interception.py tests/test_call_subagent_with_auto_answer.py -v`
Expected: 全部 PASS（8 + 20 + 7，含 Task 1 新增 2 个）

- [ ] **Step 8: 全量回归 + 代码检查**

Run: `python/bin/python -m pytest tests/ -q`
Expected: 全部 PASS（真实 LLM 集成测试在无 API key 时自动 skip，有 key 时真实跑也通过）

Run: `python/bin/python -m ruff check agent/ niu_api/`
Expected: 无新增告警

- [ ] **Step 9: Commit**

```bash
git add agent/generic/agent_loop.py niu_api/compat.py agent/runner.py tests/test_context_manager_bypass_at_prefix.py
git commit -m "fix(agent): context-manager 拦截层绕过改显式开关——模式一走标准 @end 结束逻辑，模式二/三保持一轮出方案"
```

---

### Task 3: `context-manager.md` 模式一章节补充结束方式

**Files:**
- Modify: `config/agents/context-manager.md:116`（模式一章节"实现"行之后）

- [ ] **Step 1: 插入"结束方式"小节**

`config/agents/context-manager.md`，把模式一章节末尾（L113-118）：

```markdown
**安全边界**：
- 受保护消息已从输入中排除，无需特殊处理

**实现**：用 `update_message` 改写冗余消息为精简版，用 `delete_messages` 删除被合并的消息

## 模式二：睡眠整理（半破坏性）
```

改为：

```markdown
**安全边界**：
- 受保护消息已从输入中排除，无需特殊处理

**实现**：用 `update_message` 改写冗余消息为精简版，用 `delete_messages` 删除被合并的消息

**结束方式**：
- 全部消息处理完毕后，以 `@end ` 开头输出一行总结作为最后回复，例如：
  `@end 整理完成：合并 5 组确认消息，精简 3 个大工具输出，共处理 42 条消息`
- 禁止以普通文本或空内容直接结束——程序会判定为格式错误并要求重新输出，浪费 token
- 模式一禁止输出 keep=/update= 方案文本（那是模式二/三的格式）；模式一的所有改动必须通过 update_message/delete_messages 工具落库
- 不需要向主 Agent 提问（程序触发，无人回答）；遇到无法决策的情况自行判断后继续，或用 `@end` 汇报现状结束

## 模式二：睡眠整理（半破坏性）
```

- [ ] **Step 2: 验证**

Run: `python/bin/python -m pytest tests/test_context_manager_bypass_at_prefix.py::test_context_manager_system_prompt_has_no_at_niu_guide -v`
Expected: PASS——守则注入排除逻辑不变（@end 说明写在 md 模式一章节内，不是注入的 `_SUBAGENT_ASK_GUIDE_TEMPLATE`，不含 `@niu-agent` 字样，该测试断言 `"@niu-agent" not in static_system` 仍成立）

Run: `grep -n "@end" config/agents/context-manager.md`
Expected: 能匹配到模式一章节新增的"结束方式"小节内容

- [ ] **Step 3: Commit**

```bash
git add config/agents/context-manager.md
git commit -m "docs(config): context-manager 模式一章节补充 @end 结束方式说明"
```

---

## 用户验收（真实环境，由用户执行）

单元测试只验证分支逻辑，以下真实行为由用户验收：

1. 重启程序，等待睡眠触发（或手动触发睡眠整理）
2. 观察 `logs/raw_http/YYYYMMDD/` 模式一压缩日志：多轮 `update_message`/`delete_messages` 工具调用后，最后一轮应以 `@end ` 开头的总结收尾
3. 若再遇 LLM 空响应（thinking 泄漏），应看到 FORMAT_ERROR 追问后 LLM 继续输出，而非提前退出；压缩完成后游标才推进
4. 模式二/三（上下文占用 ≥50% / ≥80% 触发）行为不变：仍一轮出 `keep=/update=` 方案，无 FORMAT_ERROR 追问、无第二轮
