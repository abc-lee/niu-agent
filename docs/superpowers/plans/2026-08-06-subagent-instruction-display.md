# 子 Agent 指令显示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让子 Agent 对话页在子 Agent 启动时，显示主 Agent 传递给它的 `task` 指令作为首条消息，使子 Agent tab 内容完整呈现"主 Agent 指令 → 子 Agent 思考/工具调用/回复"的完整对话。

**Architecture:** 在子 Agent 循环封装函数 `_run_agent_loop`（`agent/subagent.py`）开头，当 `initial_user_content` 非空且 handler 已绑定 `_subagent_unique_name` 时，推送一条 `instruction` 事件到 SubagentEventBus。前端（`ui/main/windows/assistant/chat.html`）SSE 事件 switch 新增 `instruction` case，复用 `.message.user` 样式渲染为子 Agent tab 的首条消息。事件类型用新名 `instruction`（不复用已被占用的 `user`）以避免与"用户补充信息回声"语义冲突。

**Tech Stack:** Python 3.11（Agent 后端）、FastAPI + asyncio（SSE 事件总线）、原生 JS（Electron 渲染进程 chat.html）

---

## 关键技术约束

1. **禁止子 Agent 跑全量测试**：实施过程中只能跑定向单测（`python/bin/python -m pytest tests/test_subagent_instruction.py -v`），禁止 `python/bin/python -m pytest tests/`。
2. **DTZ 时区规则**：本项目有意使用原生 `datetime`（本地时间），不引入 `timezone`。本计划不涉及时间，无需关注。
3. **前端代码修改铁律**：`ui/main/windows/assistant/chat.html` 是 3091 行单文件，只追加 `case 'instruction':` 分支（L2978 switch 内），不修改现有代码。
4. **循环 import 风险**：`agent/subagent.py` **不能**模块级 import `agent.handler._push_subagent_event`（`agent/handler.py` 已在函数内延迟 import `agent.subagent`，反向模块级 import 会循环）。后端推送必须用局部 import 模式（与 `_run_agent_loop` 现有 L282/290 块一致）。
5. **事件类型不复用 `user`**：前端 `case 'user'`（L3031-3033）已存在，注释明确"用户自己的补充信息回声，已在本地渲染过，跳过"。若后端推 `user` 事件表示主 Agent 指令，会被前端跳过。必须用新事件类型 `instruction`。

## 文件结构

- **Modify**: `agent/subagent.py` — 在 `_run_agent_loop`（L201-300）开头新增指令推送逻辑；提取为模块级纯函数 `_maybe_push_subagent_instruction(handler, initial_user_content)` 保证可单测，不依赖 `agent_runner_loop`。
- **Modify**: `ui/main/windows/assistant/chat.html` — SSE 事件 switch（L2978）新增 `case 'instruction':` 分支，复用 `addSubagentMessageToTab(unique_name, 'user', event.content)` 渲染。
- **Create**: `tests/test_subagent_instruction.py` — 单测 `_maybe_push_subagent_instruction` 的条件分支（不调真实 LLM，纯逻辑守卫）。

## 推送位置精确锚点

`agent/subagent.py` `_run_agent_loop` 函数当前结构（L241-264）：

```python
    if initial_user_content is None:                    # L241
        initial_user_content = user_input               # L242

    gen = agent_runner_loop(                            # L244
        client=client,
        ...
    )
    result = ""                                         # L264
    last_reply = ""
```

**推送插入点**：L242 之后、L244 `gen = agent_runner_loop(...)` 之前——确保指令是子 Agent 生命周期的第一个事件（在 reply/thinking/tool_status 等输出事件之前）。

---

## Task 1: 提取并实现后端指令推送纯函数

**Files:**
- Modify: `agent/subagent.py`（在 `_run_agent_loop` 之前，约 L200 处新增模块级函数；在 L242 之后新增一行调用）
- Test: `tests/test_subagent_instruction.py`

**设计决策**：把推送逻辑提取为模块级纯函数 `_maybe_push_subagent_instruction(handler, initial_user_content) -> bool`。原因：
- `_run_agent_loop` 依赖 `agent_runner_loop`（真实 LLM 生成器），直接单测它需要 mock 整个生成器，违反"测试用真实 LLM"且过度复杂。
- 提取后纯函数只测条件分支逻辑（不测 LLM 行为），属于合法的逻辑守卫测试。
- 与现有 `_strip_at_prefix`、`_extract_unique_name` 等模块级辅助函数风格一致。

- [ ] **Step 1: 写失败测试 — 函数存在性 + 签名**

创建 `tests/test_subagent_instruction.py`：

```python
"""子 Agent 指令推送纯函数守卫测试。

不调真实 LLM，只验证 _maybe_push_subagent_instruction 的条件分支逻辑。
"""
from unittest.mock import MagicMock


def test_function_exists_and_returns_bool():
    """函数存在且返回 bool。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    result = _maybe_push_subagent_instruction(handler, "处理这个文件")
    assert isinstance(result, bool)


def test_pushes_when_instruction_and_unique_name_present():
    """有指令 + handler 有 _subagent_unique_name → 推送，返回 True。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    result = _maybe_push_subagent_instruction(handler, "请处理 ~/test.txt")
    assert result is True


def test_skips_when_instruction_empty():
    """指令为空字符串 → 不推送，返回 False（续答路径 initial_user_content=None → user_input=''）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    result = _maybe_push_subagent_instruction(handler, "")
    assert result is False


def test_skips_when_instruction_none():
    """指令为 None → 不推送，返回 False。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    result = _maybe_push_subagent_instruction(handler, None)
    assert result is False


def test_skips_when_no_unique_name():
    """handler 无 _subagent_unique_name → 不推送，返回 False（主 Agent 误调用防护）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    # 显式置 None 模拟未设置（比 del 更稳健：不依赖 MagicMock 的 del 语义）
    handler._subagent_unique_name = None
    result = _maybe_push_subagent_instruction(handler, "请处理文件")
    assert result is False


def test_skips_when_unique_name_empty_string():
    """_subagent_unique_name 为空字符串 → 不推送，返回 False。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = ""
    result = _maybe_push_subagent_instruction(handler, "请处理文件")
    assert result is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python/bin/python -m pytest tests/test_subagent_instruction.py -v`
Expected: 6 个测试全部 FAIL，错误为 `ImportError: cannot import name '_maybe_push_subagent_instruction' from 'agent.subagent'`

- [ ] **Step 3: 实现纯函数**

在 `agent/subagent.py` 的 `_run_agent_loop` 函数定义**之前**（`_run_agent_loop` 的 `def` 在 L201，它的 docstring 从 L202 起；模块级函数 `_maybe_push_subagent_instruction` 插入到 L201 之前），插入：

```python
def _maybe_push_subagent_instruction(handler, initial_user_content) -> bool:
    """推送主 Agent 指令到子 Agent 前端 tab 作为首条消息。

    在 _run_agent_loop 开头调用，确保指令是子 Agent 生命周期的第一个事件。
    用局部 import 避免与 agent.handler 循环依赖（handler.py 函数内延迟 import subagent.py）。

    Args:
        handler: NiuHandler 实例，需有 _subagent_unique_name 属性。
        initial_user_content: 主 Agent 传给子 Agent 的指令文本（call_subagent 的 task 参数）。

    Returns:
        True 表示已推送，False 表示跳过（指令为空或无 unique_name）。
    """
    if not initial_user_content:
        return False
    unique_name = getattr(handler, '_subagent_unique_name', None)
    if not unique_name:
        return False
    try:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
        notify_subagent_event_sync(unique_name, 'instruction', {'content': initial_user_content})
    except ImportError:
        pass  # niu_api 未启动（如纯 Agent 单测环境），静默降级
    except Exception:
        pass  # 推送失败不影响子 Agent 循环
    return True
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python/bin/python -m pytest tests/test_subagent_instruction.py -v`
Expected: 6 个测试全部 PASS

- [ ] **Step 5: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 6: 提交**

```bash
git add agent/subagent.py tests/test_subagent_instruction.py
git commit -m "feat: add _maybe_push_subagent_instruction pure function for subagent instruction push

Extracted as module-level pure function for testability (avoids mocking
agent_runner_loop generator). Pushes 'instruction' event to SubagentEventBus
when initial_user_content is non-empty and handler has _subagent_unique_name."
```

---

## Task 2: 在 _run_agent_loop 中调用推送函数

**Files:**
- Modify: `agent/subagent.py:241-244`（在 `initial_user_content` 解析后、`gen = agent_runner_loop(...)` 之前插入一行调用）

- [ ] **Step 1: 读取当前代码确认锚点**

Run: `python/bin/python -c "
import re
src = open('agent/subagent.py').read()
# 定位 _run_agent_loop 内 L241-244 区域
lines = src.split('\n')
for i, line in enumerate(lines[238:246], start=239):
    print(f'{i}: {line}')
"`
Expected: 输出包含：
```
241:     if initial_user_content is None:
242:         initial_user_content = user_input
243: 
244:     gen = agent_runner_loop(
```

- [ ] **Step 2: 插入调用**

在 `agent/subagent.py` 的 `_run_agent_loop` 中，L242（`initial_user_content = user_input`）之后、L244（`gen = agent_runner_loop(`）之前，插入：

```python
    # 推送主 Agent 指令到子 Agent 前端 tab 作为首条消息（早于 reply/thinking/tool_status 等输出事件）
    _maybe_push_subagent_instruction(handler, initial_user_content)
```

完整上下文（修改后应为）：

```python
    if initial_user_content is None:
        initial_user_content = user_input

    # 推送主 Agent 指令到子 Agent 前端 tab 作为首条消息（早于 reply/thinking/tool_status 等输出事件）
    _maybe_push_subagent_instruction(handler, initial_user_content)

    gen = agent_runner_loop(
```

- [ ] **Step 3: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); print('OK')"`
Expected: 输出 `OK`

- [ ] **Step 4: 回归 Task 1 测试**

Run: `python/bin/python -m pytest tests/test_subagent_instruction.py -v`
Expected: 6 个测试全部 PASS（纯函数未变，确保未破坏）

- [ ] **Step 5: 提交**

```bash
git add agent/subagent.py
git commit -m "feat: call _maybe_push_subagent_instruction at start of _run_agent_loop

Instruction event pushed before agent_runner_loop starts, ensuring it's the
first event in the subagent tab. Covers both sync (L940) and async (L911)
paths; answer-resume path (L873, initial_user_content=None) is skipped by
the empty-check guard."
```

---

## Task 3: 前端渲染 instruction 事件

**Files:**
- Modify: `ui/main/windows/assistant/chat.html:2978-3036`（SSE 事件 switch 新增 `case 'instruction':` 分支）

**前端渲染决策**：复用 `addSubagentMessageToTab(unique_name, 'user', event.content)` 渲染——`.message.user` 样式（L152）已存在，视觉上与"用户输入"一致（主 Agent 指令对子 Agent 而言是输入方）。不新建 CSS 类，避免无谓样式膨胀。语义区分靠事件类型 `instruction`，不靠 CSS 类。

- [ ] **Step 1: 读取当前 switch 确认锚点**

读取 `ui/main/windows/assistant/chat.html` 的 L2976-3037 区域，确认 switch 结构。当前结构（关键行）：

```javascript
      window.electronAPI.onSubagentEvent(({ unique_name, event }) => {
        switch (event.type) {
          case 'tool_status':
            ...
            break;
          case 'thinking_chain':
            ...
            break;
          case 'reply':
            ...
            break;
          case 'question':
            ...
            break;
          case 'subagent_suspended':
            ...
            break;
          case 'subagent_error':
            ...
            break;
          case 'subagent_closed':
            ...
            break;
          case 'reconnected':
            ...
            break;
          case 'user':
            // 用户自己的补充信息回声，已在本地渲染过，跳过
            break;
          default:
            // persist / system / tool_marker 是内部事件，不展示给用户
            break;
        }
```

- [ ] **Step 2: 插入 `case 'instruction':` 分支**

在 `case 'user':` 分支**之前**（即 `case 'reconnected':` 的 `break;` 之后、`case 'user':` 之前）插入新分支。修改后该区域应为：

```javascript
          case 'reconnected':
            addSubagentMessageToTab(unique_name, 'system', '连接已恢复，正在补发历史事件...');
            break;
          case 'instruction':
            // 主 Agent 传给子 Agent 的指令，渲染为子 Agent tab 首条消息
            addSubagentMessageToTab(unique_name, 'user', event.content);
            break;
          case 'user':
            // 用户自己的补充信息回声，已在本地渲染过，跳过
            break;
```

**注意**：只追加 `case 'instruction':` 三行（含注释），不修改 `case 'user':` 及其他现有分支。

- [ ] **Step 3: JS 语法检查**

Run: `node -c ui/main/windows/assistant/chat.html && echo "SYNTAX OK" || echo "node -c 对 HTML 文件可能不适用，跳过"`
说明：`node -c` 只检查纯 JS 文件，HTML 内嵌 JS 无法直接用。改用浏览器开发者工具或手工核对。备选：把 switch 区域 JS 抽出来用 `node -c` 验证。

备选验证（推荐）：
```bash
python/bin/python -c "
import re
src = open('ui/main/windows/assistant/chat.html').read()
# 提取 onSubagentEvent 回调内的 switch 块粗略检查括号平衡
m = re.search(r'onSubagentEvent\(\(\{ unique_name, event \}\) => \{.*?switch \(event\.type\) \{', src, re.DOTALL)
print('switch found:', bool(m))
# 检查 instruction case 存在
print('instruction case found:', \"case 'instruction':\" in src)
"
```
Expected: 输出 `switch found: True` 和 `instruction case found: True`

- [ ] **Step 4: 提交**

```bash
git add ui/main/windows/assistant/chat.html
git commit -m "feat: render instruction event as first message in subagent tab

New SSE event case 'instruction' renders main agent's task directive using
existing .message.user style. Placed before 'user' case to avoid semantic
collision (user case = local user supplement echo, skipped)."
```

---

## Task 4: 端到端手工验证（需用户参与）

**Files:** 无（验证步骤）

**验证前提**：Niu Agent 已启动（`./niu`），主对话窗口可用，有可调用的子 Agent（如 `file-processor`）。

- [ ] **Step 1: 启动 Niu Agent**

```bash
./niu
```

- [ ] **Step 2: 触发子 Agent 调用**

在主对话窗口输入会触发子 Agent 的指令，例如：
```
帮我用 file-processor 处理 ~/Desktop/test.txt
```
（或任何会触发 `chat-with-file-processor` 工具的自然语言指令）

- [ ] **Step 3: 验证子 Agent tab 首条消息**

预期：
1. 子 Agent tab 自动创建（已有功能）
2. tab 内**首条消息**显示主 Agent 的指令文本（`.message.user` 样式，右对齐或用户气泡样式）
3. 随后才是子 Agent 的 thinking_chain / tool_status / reply 等输出事件

**验收标准**：
- ✅ 子 Agent tab 显示主 Agent 指令作为首条消息
- ✅ 指令内容 = `call_subagent` 的 `task` 参数（即主 Agent 决定传给子 Agent 的文本）
- ✅ 指令出现在所有子 Agent 输出事件之前
- ✅ 续答场景（子 Agent @user 提问后用户回答）不重复显示指令（因为 answer 路径 `initial_user_content=None`，`_maybe_push_subagent_instruction` 返回 False）

- [ ] **Step 4: 验证续答场景不重复推指令**

在 Step 2 的子 Agent 运行中，若子 Agent 发起 @user 提问，用户回答后观察：
- 预期：子 Agent tab **不**新增"主 Agent 指令"消息（因为续答走 `call_subagent(answer=..., initial_user_content=None)` 路径，`_maybe_push_subagent_instruction` 检测到 `initial_user_content` 为空返回 False）
- 用户回答本身走 `route_to_subagent` → supplement_queue，不经过 `_run_agent_loop` 开头的推送

- [ ] **Step 5: 验证断线重连补发**

在子 Agent 运行中关闭子 Agent tab 再重新打开（若 UI 支持），或刷新主窗口：
- 预期：ring buffer 补发时 `instruction` 事件也被补发（`notify_subagent_event_sync` 写入 `_ring_buffers`，L56-57），tab 重建后首条消息仍是主 Agent 指令
- 若 UI 不支持重连补发验证，跳过此步并记录

---

## Self-Review

### 1. Spec coverage（需求覆盖）

需求："子 Agent 页无法显示主 Agent给它的指令和与它的对话内容" → 修复"显示主 Agent 给它的指令"部分。

- ✅ 主 Agent 指令显示 → Task 1+2（后端推送）+ Task 3（前端渲染）+ Task 4（验证）
- ⚠️ "与它的对话内容"中的"对话内容"——子 Agent 的 reply/thinking/tool_status 已有（现有功能），本计划聚焦"主 Agent 指令"这一缺失方向。若"对话内容"还指其他（如子 Agent 之间的交互），需另行确认，不在本计划范围。

### 2. Placeholder scan（占位符扫描）

- 无 "TBD/TODO/implement later"
- 所有代码块完整
- 测试代码完整（6 个测试函数）
- 无"add appropriate error handling"等空泛描述（error handling 已在纯函数 try/except 中明确）

### 3. Type consistency（类型/命名一致性）

- 事件类型：后端 `'instruction'`（Task 1 L notify_subagent_event_sync 调用）↔ 前端 `case 'instruction':`（Task 3）✅ 一致
- 函数名：`_maybe_push_subagent_instruction`（Task 1 定义）↔ Task 2 调用 ✅ 一致
- 参数名：`handler, initial_user_content`（Task 1 签名）↔ Task 2 调用 `_maybe_push_subagent_instruction(handler, initial_user_content)` ✅ 一致
- handler 属性：`_subagent_unique_name`（Task 1 getattr）↔ `call_subagent` L897/L937 设置的属性 ✅ 一致
- CSS 类：`'user'`（Task 3 `addSubagentMessageToTab(unique_name, 'user', ...)`）↔ `.message.user`（chat.html L152）✅ 一致

### 4. 风险点

- **ring buffer 补发**：`instruction` 事件写入 ring buffer（L56-57），断线重连会补发。但若子 Agent 已结束、tab 关闭后重连，补发的 instruction 无 tab 可渲染——`addSubagentMessageToTab` 内 `document.getElementById('messages-' + uniqueName)` 返回 null 时 `return null`（L2835），安全。
- **同步子 Agent**：同步子 Agent 也有 tab（`createSubagentTab(uniqueName, displayName, event.is_sync)`），指令会显示，符合预期。
- **程序触发子 Agent**（`call_subagent_with_auto_answer`，L1011）：也走 `call_subagent` → `_run_agent_loop`，会推 instruction 事件。但程序触发无前端 tab（无 SSE 订阅者），事件进 ring buffer 不展示，无害。

---

## 计划审查交付条件

按项目流程：本计划需经过计划审查（连续两轮零 P0/P1/P2/P3 bug）后方可实施。
