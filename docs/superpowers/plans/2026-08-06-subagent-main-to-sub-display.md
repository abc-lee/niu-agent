# 子 Agent 页主→子对话显示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让子 Agent 对话页完整显示"主 Agent → 子 Agent"方向的所有消息——包括初始指令、主 Agent 回答子 Agent 提问的后续对话——使子 Agent tab 呈现完整的"主 Agent 指令/回答 → 子 Agent 思考/提问/工具/回复"双向对话。

**Architecture:** 主 Agent → 子 Agent 方向的消息有三个注入点，目前都不推 SSE 到子 Agent tab：
1. **初始指令**：`_run_agent_loop` 开头（`initial_user_content` = `call_subagent` 的 `task` 参数）
2. **异步主 Agent 回答**：`route_to_subagent` 的 `sender='主Agent'` 分支（db_monitor 路由主 Agent 回复 @子名 消息时）
3. **同步主 Agent 回答**：`call_subagent` 的续答路径（`answer is not None` 分支，主 Agent 在工具循环里回答子 Agent 的提问后调 `call_subagent(answer=回复)` 续答）

三个点各加一次 SSE 推送（事件类型 `instruction`，复用前端 `case 'instruction'` 渲染为 `.message.user` 样式）。前端 chat.html 的 `case 'instruction'` 已在本次回退前的实施中验证可行——本次重新实施保留该前端改动。

**Tech Stack:** Python 3.11（Agent 后端）、FastAPI + asyncio（SSE 事件总线）、原生 JS（Electron 渲染进程 chat.html）

---

## 关键技术约束

1. **禁止子 Agent 跑全量测试**：只能跑定向单测，禁止 `python/bin/python -m pytest tests/`。
2. **循环 import 风险**：`agent/subagent.py` 不能模块级 import `agent.handler` 或 `niu_api` 模块级依赖——推送用局部 import `from niu_api.internal.subagent_event_bus import notify_subagent_event_sync`。
3. **事件类型统一用 `instruction`**：前端 `case 'instruction'` 渲染为 `.message.user` 样式（右对齐蓝色气泡，表示"输入方"）。不新建 CSS 类。主 Agent 的初始指令和后续回答视觉上一致（都是主→子方向的输入），用同一事件类型 + 同一样式。
4. **线程安全**：
   - `route_to_subagent` 从 db_monitor（主 loop asyncio task）调用 → `notify_subagent_event_sync` 的 `call_soon_threadsafe` 安全
   - `call_subagent` 续答路径从主 Agent 工具循环线程（可能 executor）调用 → `call_soon_threadsafe` 跨线程安全
5. **unique_name 路由**：
   - 异步路径：`route_to_subagent` 的 `target` 参数 = @消息解析出的 `<type>-<4hex>`（at_message_parser.py L12），就是 SubagentEventBus 的 unique_name
   - 同步路径：`call_subagent` 续答的 `answer_unique_name` 参数 = 子 Agent unique_name
   - 初始指令：`_run_agent_loop` 的 `handler._subagent_unique_name`

## 数据流分析（已核实）

### 子 Agent tab 当前可见的事件（已有，不改）
- `reply`：子 Agent LLM 输出（含 @niu-agent 提问文本）——`_run_agent_loop` L278-283 推送
- `thinking_chain`：子 Agent 思考链
- `tool_status`：子 Agent 工具调用状态——`handler.py` tool_before/after_callback 推送
- `question`：子 Agent 向**用户**提问——`_ask_user_impl` L1175 推送
- `subagent_suspended/error/closed`：生命周期事件

### 子 Agent tab 当前缺失的（本次要加）
- **主 Agent 初始指令**（`call_subagent` 的 `task`）——注入 `_run_agent_loop` 但不推 SSE
- **主 Agent 回答子 Agent 提问**——两条路径都不推 SSE：
  - 异步：`route_to_subagent` sender='主Agent' → `set_answer` 解除 future，**不推 SSE**
  - 同步：`call_subagent(answer=回复)` 续答 → answer 作为 user 消息注入，**不推 SSE**

### 三个推送点精确位置

**推送点 1（初始指令）**：`agent/subagent.py` `_run_agent_loop` L241-244（`initial_user_content` 解析后、`gen = agent_runner_loop` 之前）

**推送点 2（异步主 Agent 回答）**：`agent/route_to_subagent.py` L58-63（`sender == '主Agent'` 分支，`set_answer` 成功后）

**推送点 3（同步主 Agent 回答）**：`agent/subagent.py` `call_subagent` L850-893（`answer is not None` 续答路径，`_run_agent_loop` 调用之前）

## 文件结构

- **Modify**: `agent/subagent.py` — 新增 `_maybe_push_subagent_instruction` 纯函数（推送点 1+3 复用）；在 `_run_agent_loop` 开头调用（推送点 1）；在 `call_subagent` 续答路径调用（推送点 3）
- **Modify**: `agent/route_to_subagent.py` — 在 `sender == '主Agent'` 分支 `set_answer` 成功后推 SSE（推送点 2）
- **Modify**: `ui/main/windows/assistant/chat.html` — SSE 事件 switch 新增 `case 'instruction':` 分支
- **Create**: `tests/test_subagent_instruction.py` — 单测 `_maybe_push_subagent_instruction` 的条件分支 + 副作用

---

## Task 1: 后端纯函数 + 初始指令推送（推送点 1）

**Files:**
- Modify: `agent/subagent.py`（新增模块级函数 `_maybe_push_subagent_instruction` + `_run_agent_loop` 开头调用）
- Create: `tests/test_subagent_instruction.py`

**设计决策**：提取纯函数 `_maybe_push_subagent_instruction(handler_or_unique_name, content) -> bool`，推送点 1 和 3 复用。推送点 1 传 handler（从中取 unique_name），推送点 3 直接传 unique_name 字符串。函数兼容两种参数类型。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_subagent_instruction.py`：

```python
"""子 Agent 指令/回答推送纯函数守卫测试。

不调真实 LLM，只验证 _maybe_push_subagent_instruction 的条件分支 + 副作用。
"""
from unittest.mock import MagicMock, patch


def test_pushes_when_handler_has_unique_name_and_content():
    """handler 有 unique_name + 有内容 → 推送，返回 True。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, "请处理 test.txt")
    assert result is True
    mock_notify.assert_called_once_with("file-processor-a1b2", "instruction", {"content": "请处理 test.txt"})


def test_pushes_when_unique_name_string_and_content():
    """直接传 unique_name 字符串 + 有内容 → 推送（推送点 3 续答路径用）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction("file-processor-a1b2", "这是主 Agent 的回答")
    assert result is True
    mock_notify.assert_called_once_with("file-processor-a1b2", "instruction", {"content": "这是主 Agent 的回答"})


def test_skips_when_content_empty():
    """内容为空 → 不推送（续答路径 answer="" 不推）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, "")
    assert result is False
    mock_notify.assert_not_called()


def test_skips_when_content_none():
    """内容为 None → 不推送（续答路径 initial_user_content=None）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, None)
    assert result is False
    mock_notify.assert_not_called()


def test_skips_when_no_unique_name():
    """handler 无 unique_name → 不推送。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = None
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, "请处理文件")
    assert result is False
    mock_notify.assert_not_called()


def test_skips_when_unique_name_empty_string():
    """unique_name 为空字符串 → 不推送。"""
    from agent.subagent import _maybe_push_subagent_instruction
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction("", "请处理文件")
    assert result is False
    mock_notify.assert_not_called()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python/bin/python -m pytest tests/test_subagent_instruction.py -v`
Expected: 6 个全 FAIL，`ImportError: cannot import name '_maybe_push_subagent_instruction'`

- [ ] **Step 3: 实现纯函数**

在 `agent/subagent.py` 的 `_run_agent_loop` 定义之前（`def _run_agent_loop` 在 L201），插入模块级函数：

```python
def _maybe_push_subagent_instruction(handler_or_unique_name, content) -> bool:
    """推送主 Agent 指令/回答到子 Agent 前端 tab。

    在三个推送点调用：
    1. _run_agent_loop 开头（初始指令，传 handler）
    2. route_to_subagent sender='主Agent'（异步回答，传 unique_name 字符串）
    3. call_subagent 续答路径（同步回答，传 unique_name 字符串）

    兼容两种参数：handler 对象（取 _subagent_unique_name 属性）或 unique_name 字符串。

    Args:
        handler_or_unique_name: NiuHandler 实例（有 _subagent_unique_name 属性）或 unique_name 字符串。
        content: 要推送的文本（主 Agent 指令或回答）。

    Returns:
        True 表示已推送，False 表示跳过（内容为空或无 unique_name）。
    """
    if not content:
        return False
    # 兼容 handler 对象和 unique_name 字符串
    if isinstance(handler_or_unique_name, str):
        unique_name = handler_or_unique_name
    else:
        unique_name = getattr(handler_or_unique_name, '_subagent_unique_name', None)
    if not unique_name:
        return False
    try:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
        notify_subagent_event_sync(unique_name, 'instruction', {'content': content})
    except ImportError:
        pass  # niu_api 未启动，静默降级
    except Exception:
        pass  # 推送失败不影响子 Agent 循环
    return True
```

- [ ] **Step 4: 在 `_run_agent_loop` 开头调用（推送点 1）**

在 `_run_agent_loop` 的 `initial_user_content = user_input` 之后、`gen = agent_runner_loop(` 之前，插入：

```python
    # 推送主 Agent 初始指令到子 Agent tab（早于 reply/thinking/tool_status）
    _maybe_push_subagent_instruction(handler, initial_user_content)
```

- [ ] **Step 5: 运行测试 + 语法检查**

Run: `python/bin/python -m pytest tests/test_subagent_instruction.py -v` → 6 PASS
Run: `python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); print('OK')"` → OK

- [ ] **Step 6: 提交**

```bash
git add agent/subagent.py tests/test_subagent_instruction.py
git commit -m "feat: push main agent instruction to subagent tab (initial + pure function)

Add _maybe_push_subagent_instruction pure function supporting both handler
and unique_name string params. Call at start of _run_agent_loop to push
initial task as first event in subagent tab."
```

---

## Task 2: 异步主 Agent 回答推送（推送点 2）

**Files:**
- Modify: `agent/route_to_subagent.py:58-67`（`sender == '主Agent'` 分支）

- [ ] **Step 1: 读取当前代码确认锚点**

读 `agent/route_to_subagent.py` L57-67。当前 `sender == '主Agent'` 分支：

```python
    if sender == '主Agent':
        from agent.ask_main_agent import get_pending_ask_registry
        pending_ask = get_pending_ask_registry()
        if pending_ask.set_answer(target, content):
            logger.info(f"[route] 主 Agent 回答 → {target}")
            return {"status": "ok", "message": f"已回答 {target}"}
        # set_answer 失败（无 pending future），降级推 supplement_queue
        logger.warning(f"[route] {target} 无 pending ask，降级推 supplement_queue")
        sq.push(content, is_terminate=False, sender=sender)
        return {"status": "ok", "message": f"已推送补充信息到 {target}"}
```

- [ ] **Step 2: 在 `set_answer` 成功后加 SSE 推送**

在 `pending_ask.set_answer(target, content)` 成功的分支里（`logger.info` 之后、`return` 之前），加一行推送。修改后：

```python
    if sender == '主Agent':
        from agent.ask_main_agent import get_pending_ask_registry
        pending_ask = get_pending_ask_registry()
        if pending_ask.set_answer(target, content):
            logger.info(f"[route] 主 Agent 回答 → {target}")
            # 推送主 Agent 回答到子 Agent tab（异步路径）
            from agent.subagent import _maybe_push_subagent_instruction
            _maybe_push_subagent_instruction(target, content)
            return {"status": "ok", "message": f"已回答 {target}"}
        # set_answer 失败（无 pending future），降级推 supplement_queue
        logger.warning(f"[route] {target} 无 pending ask，降级推 supplement_queue")
        sq.push(content, is_terminate=False, sender=sender)
        return {"status": "ok", "message": f"已推送补充信息到 {target}"}
```

**注意**：
- `target` 就是 unique_name（at_message_parser 解析 `<type>-<4hex>` 格式，与 SubagentEventBus 一致）
- 只在 `set_answer` 成功的分支推（降级 supplement_queue 分支不推，避免重复）
- 用局部 import `from agent.subagent import _maybe_push_subagent_instruction`（route_to_subagent 不在 subagent.py 的 import 链里，无循环风险）

- [ ] **Step 3: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/route_to_subagent.py').read()); print('OK')"` → OK

- [ ] **Step 4: 回归 Task 1 测试**

Run: `python/bin/python -m pytest tests/test_subagent_instruction.py -v` → 6 PASS

- [ ] **Step 5: 提交**

```bash
git add agent/route_to_subagent.py
git commit -m "feat: push main agent reply to subagent tab (async path)

In route_to_subagent sender='主Agent' branch, after set_answer succeeds,
push the reply content to SubagentEventBus so the subagent tab shows
main agent's answer to the subagent's question."
```

---

## Task 3: 同步主 Agent 回答推送（推送点 3）

**Files:**
- Modify: `agent/subagent.py:850-893`（`call_subagent` 的 `answer is not None` 续答路径）

- [ ] **Step 1: 读取续答路径确认锚点**

读 `agent/subagent.py` L850-893。当前续答路径关键代码：

```python
    if answer is not None and answer_unique_name is not None:
        # 阶段四第三分支：回复路径——从 registry 拿回挂起 session 继续跑
        instance = SubagentRegistry.get(answer_unique_name)
        ...
        reply_text = _strip_at_prefix(answer, answer_unique_name)
        ...
        instance.suspended_handler._subagent_unique_name = answer_unique_name
        ...
        result_text, return_value, last_reply = _run_agent_loop(
            ...
            user_input="",
            initial_user_content=None,
            handler=instance.suspended_handler,
            ...
        )
```

- [ ] **Step 2: 在续答路径 `_run_agent_loop` 之前加推送**

在 `instance.suspended_handler._subagent_unique_name = answer_unique_name` 之后、`_run_agent_loop(...)` 之前，加一行推送。推送内容 = `reply_text`（剥除 @前缀后的主 Agent 回答），unique_name = `answer_unique_name`：

```python
        instance.suspended_handler._subagent_unique_name = answer_unique_name
        # 推送主 Agent 回答到子 Agent tab（同步续答路径）
        _maybe_push_subagent_instruction(answer_unique_name, reply_text)
        ...
        result_text, return_value, last_reply = _run_agent_loop(
```

**注意**：
- `reply_text` 是 `_strip_at_prefix(answer, answer_unique_name)` 的结果（已剥 @前缀的主 Agent 回答原文）
- `answer_unique_name` 是子 Agent unique_name
- 续答路径的 `_run_agent_loop` 传 `initial_user_content=None`（Task 1 的推送点 1 会跳过），所以不会重复推

- [ ] **Step 3: 语法检查 + 回归测试**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); print('OK')"` → OK
Run: `python/bin/python -m pytest tests/test_subagent_instruction.py -v` → 6 PASS

- [ ] **Step 4: 提交**

```bash
git add agent/subagent.py
git commit -m "feat: push main agent reply to subagent tab (sync resume path)

In call_subagent answer-resume path, before _run_agent_loop, push the
stripped reply_text to SubagentEventBus so the subagent tab shows main
agent's answer in sync mode too."
```

---

## Task 4: 前端渲染 instruction 事件

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:2978-3036`（SSE 事件 switch 新增 `case 'instruction':` 分支）

- [ ] **Step 1: 读取当前 switch 确认锚点**

读 `ui/main/windows/assistant/chat.html` 的 `onSubagentEvent` 回调（约 L2976-3037）。确认 `case 'reconnected':` 的 `break;` 之后是 `case 'user':`。

- [ ] **Step 2: 插入 `case 'instruction':` 分支**

在 `case 'user':` 之前插入：

```javascript
          case 'instruction':
            // 主 Agent 传给子 Agent 的指令/回答，渲染为子 Agent tab 消息
            addSubagentMessageToTab(unique_name, 'user', event.content);
            break;
```

- [ ] **Step 3: 验证**

Run: `python/bin/python -c "
import re
src = open('ui/main/windows/assistant/chat.html').read()
print('switch found:', bool(re.search(r'onSubagentEvent.*switch \(event\.type\) \{', src, re.DOTALL)))
print('instruction case found:', \"case 'instruction':\" in src)
"`
Expected: 两个 True

- [ ] **Step 4: 提交**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat: render instruction event in subagent tab

New SSE event case 'instruction' renders main agent's directive/reply
using existing .message.user style. Covers initial instruction + async
reply + sync reply."
```

---

## Task 5: 端到端手工验证（需用户参与）

- [ ] **Step 1: 启动 Niu Agent**（`./niu`）
- [ ] **Step 2: 触发异步子 Agent**（如 file-processor 处理文件）
  - 验证：子 Agent tab 首条 = 主 Agent 指令；子 Agent 提问后，主 Agent 回答也显示在 tab
- [ ] **Step 3: 触发同步子 Agent**（如 event-manager 创建提醒）
  - 验证：子 Agent tab 首条 = 主 Agent 指令；若子 Agent ask_main_agent，主 Agent 回答（续答）也显示
- [ ] **Step 4: 验证续答不重复**（子 Agent @user 提问后用户回答，不重复推指令）

---

## Self-Review

### 1. Spec coverage
- 初始指令显示 → Task 1（推送点 1）✅
- 异步主 Agent 回答显示 → Task 2（推送点 2）✅
- 同步主 Agent 回答显示 → Task 3（推送点 3）✅
- 前端渲染 → Task 4 ✅
- 端到端验证 → Task 5 ✅

### 2. Placeholder scan
- 无 TBD/TODO；所有代码块完整；测试代码完整（6 测试含副作用 patch）

### 3. Type consistency
- 事件类型：后端 `'instruction'` ↔ 前端 `case 'instruction':` ✅
- 函数名：`_maybe_push_subagent_instruction`（Task 1 定义）↔ Task 2/3 调用 ✅
- 参数：Task 1 传 handler；Task 2/3 传 unique_name 字符串——函数兼容两种（isinstance str 判断）✅

### 4. 风险点
- **route_to_subagent 局部 import agent.subagent**：route_to_subagent.py 当前不 import subagent.py。局部 import 安全（无循环：subagent.py 不 import route_to_subagent.py）。
- **同步续答 reply_text 可能为空**：`_strip_at_prefix` 找不到前缀时原样返回（不返回空），`reply_text` 至少 = answer 原文。空 answer 被 `if not content: return False` 跳过。
- **异步 target 匹配**：at_message_parser 解析的 target = `<type>-<4hex>` = unique_name，与 SubagentEventBus 一致。同步子 Agent 的续答用 `answer_unique_name`（= agent_name，同步路径无 hex），也能匹配 SubagentEventBus（同步子 Agent unique_name = agent_name）。

---

## 计划审查交付条件
按项目流程：本计划需经过计划审查（连续两轮零 bug）后方可实施。
