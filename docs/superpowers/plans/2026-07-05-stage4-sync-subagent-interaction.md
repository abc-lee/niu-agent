# 阶段四：同步子 Agent 交互通道实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让同步调用的子 Agent 也能用 `@niu-agent` / `@end` 前缀表达意图，与主 Agent 对话时底层走 MCP 工具返回值通道，消息格式与异步路径完全一致。

**Architecture:** 复用阶段三异步路径已实现的 unique_name 包装机制（`_ask_main_agent_impl` subagent.py:810），同步路径新增 `_ask_main_agent_impl_sync` 做同样包装但不阻塞。同步子 Agent 输出 `@niu-agent 问题` 时，拦截层调 `_ask_main_agent_impl_sync` 包装成 `[unique_name] 问题` 作为 yield reply 返回给主 Agent；主 Agent 重调 `chat-with-xxx(answer, unique_name)`，call_subagent 第三分支从 SubagentRegistry 拿回挂起 session，注入回答后用 `resumed_messages` 重新调 `_run_agent_loop`。所有子 Agent（同步+异步）强制注入 @niu-agent/@end 守则。程序触发子 Agent 由 `call_subagent_with_auto_answer` helper 自动回复。

**Tech Stack:** Python（agent_loop.py / subagent.py / subagent_registry.py / handler.py / runner.py / compat.py），纯内存 SubagentRegistry，OpenAI tool schema。

**Spec:** `docs/superpowers/specs/2026-07-05-stage4-sync-subagent-interaction-design.md`（v14）

---

## 文件结构

### 新建文件
- `tests/test_sync_subagent_interaction.py` — 同步子 Agent 交互单元测试
- `tests/test_call_subagent_with_auto_answer.py` — helper 单元测试

### 修改文件
- `agent/subagent_registry.py` — RunningSubagent 新增 6 字段
- `agent/generic/agent_loop.py` — 拦截层改造（条件 + tuple 返回 + INTERCEPTED_SYNC 分支）+ resumed_messages 参数 + _fifo_prune is_resumed 参数 + §9A 改名
- `agent/subagent.py` — 守则注入恢复 + _ask_main_agent_impl_sync + call_subagent 三分支 + finally 条件化 + control_flow_results 集合更新 + §9A 改名
- `agent/handler.py` — _call_subagent_gen 透传 answer + unique_name
- `agent/runner.py` — chat-with-xxx schema 加参数 + request_stop_all_subagents 改造 + cleanup_suspended_sync_subagents + 程序触发点替换
- `niu_api/compat.py` — 9 个程序触发点替换为 helper
- `config/agents/niu.md` — §9B 提示词增量 + 改名
- `config/agent-template.md` — §9B.3 增量 + 改名
- `docs/SYSTEM_MANUAL.md` / `docs/manual-general-subagent.md` — 文档同步 + 改名
- `docs/superpowers/specs/2026-07-04-at-prefix-subagent-intent.md` — 历史 spec 改名
- `tests/test_at_prefix_interception.py` — 现有断言改 tuple + 同步路径测试 + 改名
- `tests/test_ask_main_agent_stop_deadlock.py` / `tests/verify_llm_at_prefix.py` / `tests/test_ask_main_agent.py` / `tests/test_request_stop_all_subagents.py` / `tests/test_db_monitor.py` — 改名
- `tests/test_subagent_registry.py` — 新增字段测试（如不存在则新建）

---

## Task 1: §9A 全仓 @niu 改名为 @niu-agent（先做，避免后续混淆）

**Files:**
- Modify: `agent/generic/agent_loop.py` L13/54/74/75/76/77/79/84/97/126/577
- Modify: `config/agents/niu.md` L255/283/291
- Modify: `config/agent-template.md` L27/70
- Modify: `tests/test_at_prefix_interception.py` L60/83/98/165/207/215/237
- Modify: `tests/test_ask_main_agent_stop_deadlock.py` L1/4/36/65/71/85/97/115/128/138
- Modify: `tests/verify_llm_at_prefix.py` L1/22/26/30/105/112/113
- Modify: `tests/test_ask_main_agent.py` L95/180/182/183
- Modify: `tests/test_request_stop_all_subagents.py` L1/10
- Modify: `tests/test_db_monitor.py` L39
- Modify: `docs/SYSTEM_MANUAL.md` L348
- Modify: `docs/manual-general-subagent.md` L17/86/117
- Modify: `docs/superpowers/specs/2026-07-04-at-prefix-subagent-intent.md`（61 处）

- [ ] **Step 1: agent_loop.py 加 _AT_NIU_PREFIX 常量 + 改 L75/77/84/97/126**

在 `agent/generic/agent_loop.py` 顶部（L10 `_VALID_STREAM_TYPES` 之后）加常量：

```python
_AT_NIU_PREFIX = "@niu-agent"  # 子 Agent 询问主 Agent 的 content 前缀（10 字符）
```

改 L75：
```python
# 旧
if stripped.startswith("@niu"):
# 新
if stripped.startswith(_AT_NIU_PREFIX):
```

改 L77：
```python
# 旧
question = stripped[4:].lstrip()
# 新
question = stripped[len(_AT_NIU_PREFIX):].lstrip()
```

改 L84/97/126 FORMAT_ERROR 注入文本里的 `@niu` 全改为 `@niu-agent`（共 3 处，每处含 "询问主 Agent：content 以 `@niu ` 开头" 改为 "@niu-agent "）。

改 L13 注释 `# @niu 拦截成功` → `# @niu-agent 拦截成功`。
改 L54 docstring `content 以 @niu 开头` → `content 以 @niu-agent 开头`。
改 L74 注释 `# @niu 拦截` → `# @niu-agent 拦截`。
改 L76 注释 `# 剥除 "@niu" 前缀` → `# 剥除 "@niu-agent" 前缀`。
改 L79 日志 `[AtPrefix] @niu 后无问题内容` → `[AtPrefix] @niu-agent 后无问题内容`。
改 L577 注释 `# @niu 已处理` → `# @niu-agent 已处理`。

- [ ] **Step 2: 跑现有测试确认改名无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_at_prefix_interception.py -v 2>&1 | tail -20
```

Expected: 多数测试 FAIL（因为测试输入还是 `@niu`，下一步改测试输入）。

- [ ] **Step 3: 改 tests/test_at_prefix_interception.py 测试输入 + 断言**

把所有 `@niu` 测试输入字符串和断言改为 `@niu-agent`。具体：
- L60: `content="@niu 我应该..."` → `content="@niu-agent 我应该..."`
- L83: `content="@niu 我应该选择哪个选项？"` → `content="@niu-agent 我应该选择哪个选项？"`
- L98: `assert messages[-2]["content"] == "@niu 我应该..."` → `== "@niu-agent 我应该..."`
- L165: `assert "@niu" in messages[-1]["content"]` → `assert "@niu-agent" in ...`
- L207/215/237: `content="@niu"` / `content="@niu 问题"` → `@niu-agent`

- [ ] **Step 4: 跑测试确认通过**

```bash
python -m pytest tests/test_at_prefix_interception.py -v 2>&1 | tail -20
```

Expected: PASS。

- [ ] **Step 5: 改其余 5 个测试文件**

`tests/test_ask_main_agent_stop_deadlock.py` L1/4/36/65/71/85/97/115/128/138：所有 `@niu` 改 `@niu-agent`（含 L71 测试输入 `"@niu 这个 PDF 是扫描件吗？"` → `"@niu-agent 这个 PDF 是扫描件吗？"`）。

`tests/verify_llm_at_prefix.py` L1/22/26/30/105/112/113：`@niu` → `@niu-agent`，`startswith("@niu")` → `startswith("@niu-agent")`。

`tests/test_ask_main_agent.py` L95/180/182/183：`@niu` → `@niu-agent`。

`tests/test_request_stop_all_subagents.py` L1/10：`@niu` → `@niu-agent`。

`tests/test_db_monitor.py` L39：`@niu` → `@niu-agent`。

- [ ] **Step 6: 改 config/agents/niu.md L255/283/291**

把 L255/L283/L291 里的 `@niu` 改为 `@niu-agent`（共 3 处）。

- [ ] **Step 7: 改 config/agent-template.md L27/70**

把 L27/L70 里的 `@niu` 改为 `@niu-agent`（共 2 处）。

- [ ] **Step 8: 改 docs/SYSTEM_MANUAL.md L348 + docs/manual-general-subagent.md L17/86/117**

`@niu` → `@niu-agent`（共 4 处）。

- [ ] **Step 9: 改 docs/superpowers/specs/2026-07-04-at-prefix-subagent-intent.md**

全文 61 处 `@niu` → `@niu-agent`（用 sed 或 Python 脚本，注意保留"改名前"的描述性字面量，但本文件是历史 spec，全改）。

- [ ] **Step 10: 验证 db_monitor 路由不误伤**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && grep -n "at_message_parser\|_AT_PATTERN" agent/at_message_parser.py | head -5
```

确认 `at_message_parser.py:12` 正则 `@([a-z]+(?:-[a-z]+)*-[0-9a-f]{4})\s+` 要求 4 位 hex 后缀，`@niu-agent` 不匹配。

- [ ] **Step 11: grep niu_api/ 确认无旧 @niu**

```bash
grep -rn "@niu[^-]" REDACTED_USER_PATH/tools/ai-bot/niu_api/ 2>/dev/null | head
```

Expected: 无输出（或仅 logs/ 历史产物）。

- [ ] **Step 12: 跑全量测试确认改名无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: 所有测试 PASS。

- [ ] **Step 13: 启动程序确认无 import 错误**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && ./niu 2>&1 | head -20 &
sleep 5 && ps aux | grep -i niu | head -5 && kill %1 2>/dev/null
```

Expected: 进程启动无 import 错误。

- [ ] **Step 14: Commit**

```bash
git add agent/generic/agent_loop.py agent/subagent.py config/ tests/ docs/
git commit -m "refactor(at-prefix): 全仓 @niu 改名为 @niu-agent

避免知识图谱根节点 niu 被误连。改名范围：拦截层代码 + 提示词 +
测试 + 文档 + 历史 spec。db_monitor 路由已确认安全（正则要求
4 位 hex 后缀，@niu-agent 不匹配）。"
```

---

## Task 2: SubagentRegistry 字段扩展

**Files:**
- Modify: `agent/subagent_registry.py:21-32`
- Test: `tests/test_subagent_registry.py`

- [ ] **Step 1: 写失败测试——新增字段默认值**

在 `tests/test_subagent_registry.py` 加测试（若文件不存在则新建）：

```python
def test_running_subagent_default_fields():
    """RunningSubagent 新增 6 字段默认值正确"""
    from agent.subagent_registry import RunningSubagent
    from agent.subagent_supplement import SubagentSupplementQueue
    sq = SubagentSupplementQueue(unique_name="")
    r = RunningSubagent(unique_name="test-ab12", agent_type="test", supplement_queue=sq)
    assert r.state == "running"
    assert r.suspended_messages is None
    assert r.suspended_handler is None
    assert r.suspended_client is None
    assert r.suspended_tools_schema is None
    assert r.suspended_system_message is None
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_subagent_registry.py::test_running_subagent_default_fields -v 2>&1 | tail -10
```

Expected: FAIL with `AttributeError: ... has no attribute 'state'` 或类似。

- [ ] **Step 3: 修改 RunningSubagent 数据类**

`agent/subagent_registry.py:21-32` 改为：

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

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_subagent_registry.py::test_running_subagent_default_fields -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 5: 写失败测试——state 转换**

```python
def test_running_subagent_state_transition():
    """state 字段可被外部修改"""
    from agent.subagent_registry import RunningSubagent
    from agent.subagent_supplement import SubagentSupplementQueue
    sq = SubagentSupplementQueue(unique_name="")
    r = RunningSubagent(unique_name="test-ab12", agent_type="test", supplement_queue=sq)
    r.state = "waiting_for_answer"
    assert r.state == "waiting_for_answer"
    r.state = "running"
    assert r.state == "running"
```

- [ ] **Step 6: 跑测试确认 PASS**

```bash
python -m pytest tests/test_subagent_registry.py::test_running_subagent_state_transition -v 2>&1 | tail -10
```

Expected: PASS（dataclass 字段默认可变）。

- [ ] **Step 7: Commit**

```bash
git add agent/subagent_registry.py tests/test_subagent_registry.py
git commit -m "feat(subagent_registry): RunningSubagent 新增 6 字段

state / suspended_messages / suspended_handler / suspended_client /
suspended_tools_schema / suspended_system_message——同步 @niu-agent
挂起状态存储"
```

---

## Task 3: 守则注入恢复（所有子 Agent 统一注入）

**Files:**
- Modify: `agent/subagent.py`（重新引入 `_SUBAGENT_ASK_GUIDE_TEMPLATE` / `_SUBAGENT_ASK_GUIDE_MARKER` + `build_subagent_system_segments` 统一注入）
- Test: `tests/test_general_subagent.py`（现有文件，加测试）

- [ ] **Step 1: 写失败测试——所有子 Agent 都注入守则**

在 `tests/test_general_subagent.py` 加测试：

```python
def test_build_subagent_system_segments_injects_guide_for_all_subagents(tmp_path, monkeypatch):
    """所有子 Agent（同步+异步）build_subagent_system_segments 都注入 @niu-agent/@end 守则"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text("---\ndescription: my agent\n---\nYou are my agent.")

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    static_system, dynamic_system = subagent.build_subagent_system_segments("my-agent")
    assert "<!-- NIU_SUBAGENT_GUIDE_v1 -->" in static_system
    assert "@niu-agent" in static_system
    assert "@end" in static_system
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_general_subagent.py::test_build_subagent_system_segments_injects_guide_for_all_subagents -v 2>&1 | tail -10
```

Expected: FAIL with `AssertionError: assert '<!-- NIU_SUBAGENT_GUIDE_v1 -->' in ...`（守则未注入）。

- [ ] **Step 3: 在 subagent.py 引入守则常量**

在 `agent/subagent.py` 的 `_BOUNDARY_SECTION_TEMPLATE`（L66 附近）之后加：

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

- [ ] **Step 4: 修改 build_subagent_system_segments 统一注入**

`agent/subagent.py` 的 `build_subagent_system_segments`（L384 附近）在 `_BOUNDARY_SECTION_TEMPLATE` 注入之后、`Current Time` 之前加：

```python
    # 4. 强制注入 @niu-agent/@end 守则（所有子 Agent）
    if _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
        static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
```

- [ ] **Step 5: 跑测试确认 PASS**

```bash
python -m pytest tests/test_general_subagent.py::test_build_subagent_system_segments_injects_guide_for_all_subagents -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 6: 写测试——守则不重复注入**

```python
def test_build_subagent_system_segments_no_duplicate_injection(tmp_path, monkeypatch):
    """子 Agent 正文已含 marker 时不重复注入"""
    from agent import subagent

    user_dir = tmp_path / "user" / "agents"
    user_dir.mkdir(parents=True)
    (user_dir / "my-agent.md").write_text(
        "---\ndescription: my agent\n---\nYou are my agent.\n\n<!-- NIU_SUBAGENT_GUIDE_v1 -->\n已有守则"
    )

    project_dir = tmp_path / "project" / "config" / "agents"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(subagent, "_PROJECT_AGENTS_DIR", str(project_dir))
    monkeypatch.setattr(subagent, "_USER_AGENTS_DIR", str(user_dir))

    static_system, _ = subagent.build_subagent_system_segments("my-agent")
    # 守则只出现一次（marker 计数 == 1）
    assert static_system.count(_SUBAGENT_ASK_GUIDE_MARKER if hasattr(subagent, '_SUBAGENT_ASK_GUIDE_MARKER') else "<!-- NIU_SUBAGENT_GUIDE_v1 -->") == 1
```

- [ ] **Step 7: 跑测试确认 PASS**

```bash
python -m pytest tests/test_general_subagent.py::test_build_subagent_system_segments_no_duplicate_injection -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 8: Commit**

```bash
git add agent/subagent.py tests/test_general_subagent.py
git commit -m "feat(subagent): 恢复守则注入，所有子 Agent 统一注入 @niu-agent/@end

之前 commit 0ee5660f 回退了守则注入，导致子 Agent 第一次输出不知道
用 @niu 前缀。本阶段恢复，且对所有子 Agent（同步+异步）统一注入。"
```

---

## Task 4: 拦截层改造（含 `_ask_main_agent_impl_sync` 实现）

**Files:**
- Modify: `agent/generic/agent_loop.py` L12-16（常量）+ L44-130（拦截层）+ L568-593（agent_runner_loop 调用点）
- Modify: `agent/subagent.py`（在 `_ask_main_agent_impl` 旁加 `_ask_main_agent_impl_sync`）
- Test: `tests/test_at_prefix_interception.py` + `tests/test_sync_subagent_interaction.py`（新建）

**注**：本 task 合并了原 Task 4（拦截层改造）+ 原 Task 5（`_ask_main_agent_impl_sync` 实现），解决 v1 审查 B1/B4——先实现 `_ask_main_agent_impl_sync`，再改拦截层，避免 import 错误。

- [ ] **Step 1: 写失败测试——`_ask_main_agent_impl_sync`**

新建 `tests/test_sync_subagent_interaction.py`：

```python
"""同步子 Agent 交互单元测试"""


def test_ask_main_agent_impl_sync_appends_assistant_and_returns_wrapped():
    """_ask_main_agent_impl_sync 调用后 messages append assistant content + 返回 [unique_name] question"""
    from agent import subagent

    messages = [{"role": "user", "content": "开始"}]
    fake_handler = object()  # 不需要 handler 属性

    wrapped = subagent._ask_main_agent_impl_sync(
        question="我应该选择哪个选项？",
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent 我应该选择哪个选项？",
    )

    # 断言：messages append assistant content
    assert messages[-1] == {"role": "assistant", "content": "@niu-agent 我应该选择哪个选项？"}
    # 断言：返回 wrapped 文本
    assert wrapped == "[test-ab12] 我应该选择哪个选项？"
    # 断言：messages 末尾是 assistant（不是 user）
    assert len(messages) == 2
    assert messages[-1]["role"] == "assistant"


def test_ask_main_agent_impl_sync_sanitizes_question():
    """_ask_main_agent_impl_sync 对 question 做 sanitization（限 2000 字符 + strip 行首 @）"""
    from agent import subagent

    messages = []
    fake_handler = object()

    # 超长 question 截断
    long_question = "x" * 3000
    wrapped = subagent._ask_main_agent_impl_sync(
        question=long_question,
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent ...",
    )
    assert len(wrapped) < 3000  # 已截断

    # question 行首 @ 被 strip
    wrapped2 = subagent._ask_main_agent_impl_sync(
        question="@嵌套@问题",
        unique_name="test-ab12",
        handler=fake_handler,
        messages=messages,
        content="@niu-agent @嵌套@问题",
    )
    assert wrapped2 == "[test-ab12] 嵌套@问题"  # 行首 @ 被 strip
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_sync_subagent_interaction.py -v 2>&1 | tail -10
```

Expected: FAIL with `AttributeError: module 'agent.subagent' has no attribute '_ask_main_agent_impl_sync'`。

- [ ] **Step 3: 实现 `_ask_main_agent_impl_sync`**

在 `agent/subagent.py` 的 `_ask_main_agent_impl` 函数（L757-843）之后加：

```python
def _ask_main_agent_impl_sync(question: str, unique_name: str, handler, messages: list, content: str) -> str:
    """同步路径：包装 question 为 [unique_name] question，append assistant content 到 messages。

    与异步 _ask_main_agent_impl（subagent.py:810）的包装逻辑一致，但：
    - 不阻塞等主 Agent 回答（同步路径靠工具返回值通道）
    - 不推 MainAgentRequestQueue（同步路径不走 db_monitor）
    - append assistant content 保留对话历史，不 append user（user 由第二次 call_subagent 注入）
    """
    messages.append({"role": "assistant", "content": content})
    # sanitization（与异步路径 subagent.py:807-809 一致）
    sanitized = question[:2000] if question else ""
    if sanitized.lstrip().startswith("@"):
        sanitized = sanitized.lstrip()[1:]
    wrapped = f"[{unique_name}] {sanitized}"
    return wrapped
```

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_sync_subagent_interaction.py -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 5: Commit `_ask_main_agent_impl_sync`**

```bash
git add agent/subagent.py tests/test_sync_subagent_interaction.py
git commit -m "feat(subagent): _ask_main_agent_impl_sync 同步路径包装函数

复用异步 _ask_main_agent_impl 的 [unique_name] question 包装逻辑，
但不阻塞、不推 queue。append assistant content 保留对话历史。"
```

- [ ] **Step 6: 加 INTERCEPTED_SYNC 常量**

`agent/generic/agent_loop.py` L12-16 改为：

```python
# @前缀子Agent意图识别返回值
INTERCEPTED = "intercepted"          # 异步 @niu-agent 拦截成功
INTERCEPTED_SYNC = "intercepted_sync"  # 同步 @niu-agent 拦截成功
EXIT = "exit"                        # @end 允许退出
FORMAT_ERROR = "format_error"        # 无 @ 前缀无 tool_calls，已追加格式错误提示
NO_INTERCEPTION = "no_intercept"     # 不拦截（主 Agent 或有 tool_calls）
```

- [ ] **Step 2: 写失败测试——同步子 Agent @niu-agent 拦截**

在 `tests/test_at_prefix_interception.py` 加测试：

```python
def test_sync_subagent_at_niu_returns_intercepted_sync(monkeypatch):
    """同步子 Agent（_is_sync_subagent=True, memory_context=None）输出 @niu-agent → 返回 (INTERCEPTED_SYNC, wrapped)"""
    from agent.generic import agent_loop
    from agent import subagent

    monkeypatch.setattr(subagent, "_ask_main_agent_impl_sync", mock.Mock(return_value="[test-ab12] 问题"))

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-ab12"
    fake_handler._is_sync_subagent = True  # 同步子 Agent
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@niu-agent 我应该选哪个？",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 同步子 Agent
    )

    status, payload = result
    assert status == agent_loop.INTERCEPTED_SYNC
    assert payload == "[test-ab12] 问题"
```

- [ ] **Step 3: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_at_prefix_interception.py::test_sync_subagent_at_niu_returns_intercepted_sync -v 2>&1 | tail -10
```

Expected: FAIL（拦截层当前 `memory_context is None` 直接返回 NO_INTERCEPTION）。

- [ ] **Step 4: 修改拦截条件 + 拦截层返回 tuple**

`agent/generic/agent_loop.py:44-130` 改造 `_intercept_at_prefix_content`：

```python
def _intercept_at_prefix_content(
    content: str,
    tool_calls: list,
    messages: list,
    handler,
    memory_context,
) -> tuple:
    """@前缀子Agent意图识别拦截层。返回 (status, payload)。

    - (NO_INTERCEPTION, None)：主 Agent 或有 tool_calls，不拦截
    - (INTERCEPTED, None)：异步 @niu-agent 已处理（messages 已 append assistant + user）
    - (INTERCEPTED_SYNC, wrapped_text)：同步 @niu-agent，agent_runner_loop yield reply + return
    - (EXIT, None)：@end，agent_runner_loop 剥前缀 yield reply + return
    - (FORMAT_ERROR, None)：格式错误，agent_runner_loop continue
    """
    is_sync_subagent = getattr(handler, "_is_sync_subagent", False)
    # 同步子 Agent 或异步子 Agent 进入拦截层；主 Agent 不拦截
    if (memory_context is None and not is_sync_subagent) or tool_calls:
        return (NO_INTERCEPTION, None)

    stripped = (content or "").lstrip()

    # @niu-agent 拦截
    if stripped.startswith(_AT_NIU_PREFIX):
        question = stripped[len(_AT_NIU_PREFIX):].lstrip()
        if not question:
            logger.error(f"[AtPrefix] {_AT_NIU_PREFIX} 后无问题内容")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
                "1. 调用工具继续工作（正常 tool_calls）\n"
                f"2. 询问主 Agent：content 以 `{_AT_NIU_PREFIX} ` 开头，如 `{_AT_NIU_PREFIX} 我应该选择哪个选项？`\n"
                "3. 结束会话：content 以 `@end ` 开头，如 `@end 任务已完成，结果：...`\n"
                "禁止输出不带 @ 前缀的纯 content。请重新输出。"
            })
            return (FORMAT_ERROR, None)

        unique_name = getattr(handler, "_subagent_unique_name", "")
        if not unique_name:
            logger.error(f"[AtPrefix] 子 Agent 无 _subagent_unique_name")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
                "1. 调用工具继续工作（正常 tool_calls）\n"
                f"2. 询问主 Agent：content 以 `{_AT_NIU_PREFIX} ` 开头\n"
                "3. 结束会话：content 以 `@end ` 开头\n"
                "禁止输出不带 @ 前缀的纯 content。请重新输出。"
            })
            return (FORMAT_ERROR, None)

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
            return (INTERCEPTED_SYNC, wrapped)
        else:
            # 异步路径：阻塞等主 Agent 回答（现有逻辑）
            from agent.subagent import _ask_main_agent_impl
            answer = _ask_main_agent_impl(question=question, unique_name=unique_name)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"[主 Agent 回答] {answer}"})
            return (INTERCEPTED, None)

    # @end 允许退出
    if stripped.startswith("@end"):
        return (EXIT, None)

    # 格式错误
    messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content":
        "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
        "1. 调用工具继续工作（正常 tool_calls）\n"
        f"2. 询问主 Agent：content 以 `{_AT_NIU_PREFIX} ` 开头\n"
        "3. 结束会话：content 以 `@end ` 开头\n"
        "禁止输出不带 @ 前缀的纯 content。请重新输出。"
    })
    return (FORMAT_ERROR, None)
```

- [ ] **Step 5: 修改 agent_runner_loop 调用点改 tuple 解构**

`agent/generic/agent_loop.py:568-593` 改为：

```python
if not response.tool_calls:
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
    if interception_status == FORMAT_ERROR:
        _harness_fail_count = 0
        continue
    # NO_INTERCEPTION：继续走原有逻辑
```

- [ ] **Step 6: 改现有测试断言为 tuple + 显式设 _is_sync_subagent=False**

`tests/test_at_prefix_interception.py` 所有 `assert result == agent_loop.INTERCEPTED` / `EXIT` / `FORMAT_ERROR` / `NO_INTERCEPTION` 改为 `assert result == (agent_loop.XXX, None)` 或 `assert result[0] == agent_loop.XXX`。具体：
- L103: `assert result == agent_loop.INTERCEPTED` → `assert result == (agent_loop.INTERCEPTED, None)`
- L121: `assert result == agent_loop.EXIT` → `assert result == (agent_loop.EXIT, None)`
- L140: `assert result == agent_loop.EXIT` → `assert result == (agent_loop.EXIT, None)`
- L159: `assert result == agent_loop.FORMAT_ERROR` → `assert result == (agent_loop.FORMAT_ERROR, None)`
- L184: `assert result == agent_loop.NO_INTERCEPTION` → `assert result == (agent_loop.NO_INTERCEPTION, None)`
- L203: `assert result == agent_loop.NO_INTERCEPTION` → `assert result == (agent_loop.NO_INTERCEPTION, None)`
- L222: `assert result == agent_loop.FORMAT_ERROR` → `assert result == (agent_loop.FORMAT_ERROR, None)`
- L244: `assert result == agent_loop.FORMAT_ERROR` → `assert result == (agent_loop.FORMAT_ERROR, None)`
- L283: `assert result == agent_loop.NO_INTERCEPTION` → `assert result == (agent_loop.NO_INTERCEPTION, None)`

**关键修复（v1 审查 B2）**：现有测试用 `fake_handler = mock.MagicMock()`——`mock.MagicMock()` 对任何属性访问返回 truthy Mock 对象，`getattr(handler, "_is_sync_subagent", False)` 返回 truthy Mock（等同 True）。改造后拦截条件 `(memory_context is None and not is_sync_subagent)`——对 truthy Mock `not truthy_Mock` = False，条件不满足，**会进入拦截层**导致期望 NO_INTERCEPTION 的测试 fail。

所有用 `mock.MagicMock()` 作 handler 且期望 NO_INTERCEPTION 的测试（L169-185 `test_no_interception_for_sync_subagent` + L261-283 `test_main_agent_not_intercepted`），必须显式设：

```python
fake_handler = mock.MagicMock()
fake_handler._is_sync_subagent = False  # 显式设为 False，模拟主 Agent 路径
```

L169 测试名 `test_no_interception_for_sync_subagent` 语义已过时（同步子 Agent 现在要拦截），改名为 `test_main_agent_path_not_intercepted` 并设 `_is_sync_subagent=False`。

其他期望 INTERCEPTED/EXIT/FORMAT_ERROR 的测试（如 L75/L118/L137/L156/L219/L241 用 `memory_context=mock.MagicMock()` 非 None，或 L88/L165 用 `fake_handler._subagent_unique_name = "test-agent-abc1"` 但 `_is_sync_subagent` 未设）：
- `memory_context` 非 None 的测试（异步路径）——`_is_sync_subagent` 不影响（拦截条件第一部分 `memory_context is None` False，整体 False，进入拦截层）——OK，不用改。
- 但若同步路径测试（`memory_context=None`）期望 INTERCEPTED/EXIT/FORMAT_ERROR——必须显式设 `fake_handler._is_sync_subagent = True`。

具体逐个测试检查：
- L75 `test_at_niu_prefix_triggers_ask_main_agent`：`memory_context=mock.MagicMock()`（异步）——不用改 `_is_sync_subagent`。
- L88 `test_at_end_prefix_allows_exit_with_space`：`memory_context=mock.MagicMock()`（异步）——不用改。
- L118 `test_at_end_prefix_allows_exit_without_space`：同上——不用改。
- L137 `test_no_at_prefix_no_tool_calls_returns_format_error`：同上——不用改。
- L156 `test_no_interception_for_sync_subagent`：`memory_context=None` + 期望 NO_INTERCEPTION——**改为 `fake_handler._is_sync_subagent = False` + 改名 `test_main_agent_path_not_intercepted`**。
- L219 `test_at_niu_without_question_returns_format_error`：`memory_context=mock.MagicMock()`（异步）——不用改。
- L241 `test_at_niu_without_unique_name_returns_format_error`：同上——不用改。
- L261 `test_agent_runner_loop_intercepts_at_niu`：只验证 hasattr，不调拦截层——不用改。
- L275 `test_main_agent_not_intercepted`：`memory_context=None` + 期望 NO_INTERCEPTION——**改为 `fake_handler._is_sync_subagent = False`**。

- [ ] **Step 7: 跑测试确认 PASS**

```bash
python -m pytest tests/test_at_prefix_interception.py -v 2>&1 | tail -20
```

Expected: PASS（含新测试 + 现有测试改 tuple 后）。

- [ ] **Step 8: 写回归测试——主 Agent 路径不被拦截**

```python
def test_main_agent_not_intercepted_after_change(monkeypatch):
    """主 Agent 路径（_is_sync_subagent=False, memory_context=None）仍返回 (NO_INTERCEPTION, None)"""
    from agent.generic import agent_loop
    fake_handler = mock.MagicMock()
    fake_handler._is_sync_subagent = False  # 主 Agent
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="这是主 Agent 的正常回复",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )

    assert result == (agent_loop.NO_INTERCEPTION, None)
    assert len(messages) == 1  # messages 不被追加
```

- [ ] **Step 9: 跑测试确认 PASS**

```bash
python -m pytest tests/test_at_prefix_interception.py::test_main_agent_not_intercepted_after_change -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 10: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_at_prefix_interception.py
git commit -m "feat(agent_loop): 拦截层返回 tuple + INTERCEPTED_SYNC 同步分支

- 拦截条件加 is_sync_subagent 判断
- 拦截层返回 (status, payload) tuple
- 新增 INTERCEPTED_SYNC 常量
- agent_runner_loop 同步分支 yield reply + 显式 return
- 子 Agent 路径不调全局 clear_stop
- 现有测试断言改 tuple"
```

---

## Task 5: （已合并到 Task 4）

原 Task 5 内容（`_ask_main_agent_impl_sync` 实现）已合并到 Task 4，解决 v1 审查 B1/B4（Task 4 引用 Task 5 的函数导致 import 错误）。Task 4 Step 1-5 实现该函数，Step 6+ 做拦截层改造。

---

## Task 6: agent_runner_loop resumed_messages 参数 + _fifo_prune is_resumed 参数

**Files:**
- Modify: `agent/generic/agent_loop.py` L246-292（_fifo_prune）+ L391-477（agent_runner_loop 签名 + messages 构造）
- Test: `tests/test_sync_subagent_interaction.py`

- [ ] **Step 1: 写失败测试——resumed_messages 跳过 messages 构造**

在 `tests/test_sync_subagent_interaction.py` 加测试：

```python
def test_agent_runner_loop_resumed_messages_skips_construction(monkeypatch):
    """agent_runner_loop 收到 resumed_messages → 跳过 system_message + history + user_input 构造"""
    from agent.generic import agent_loop
    from agent.generic.agent_loop import StreamEvent

    # mock LLM client——必须返回生成器（agent_loop.py:557 用 exhaust(response_gen) 调 next()）
    # v1 审查 B3 修正：MagicMock 不是迭代器，next() 会抛 TypeError
    fake_response = mock.MagicMock()
    fake_response.content = "@end 任务完成"
    fake_response.tool_calls = None
    fake_response.usage = None

    def fake_chat_gen():
        """模拟流式生成器：yield 一个 chunk 后 StopIteration.value = fake_response"""
        yield
        return fake_response

    fake_client = mock.MagicMock()
    fake_client.chat.return_value = fake_chat_gen()

    fake_handler = mock.MagicMock()
    fake_handler._is_subagent = True
    fake_handler._is_sync_subagent = True  # 显式设，避免 truthy Mock 语义问题
    fake_handler._subagent_unique_name = "test-ab12"

    # resumed_messages：已是 LLM-ready 格式（含 system + 历史 + user）
    resumed = [
        {"role": "system", "content": "你是子 Agent"},
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": "@niu-agent 问题"},
        {"role": "user", "content": "[主 Agent 回答] 选 A"},
    ]

    system_message = {"role": "system", "content": "你是子 Agent"}
    gen = agent_loop.agent_runner_loop(
        client=fake_client,
        system_prompt="",
        system_message=system_message,
        user_input="不应被用",
        handler=fake_handler,
        tools_schema=[],
        max_turns=5,
        initial_user_content=None,
        context_window_tokens=100000,
        context_fifo_threshold=75000,
        context_target_threshold=30000,
        history=[],
        memory_context=None,
        resumed_messages=resumed,
    )

    events = list(gen)
    # 验证：LLM 调用时 messages 是 resumed，不含"不应被用"的 user_input
    call_kwargs = fake_client.chat.call_args
    messages_passed = call_kwargs.kwargs.get("messages", call_kwargs.args[0] if call_kwargs.args else None)
    # resumed 的最后一条是 user "[主 Agent 回答] 选 A"，不是"不应被用"
    assert messages_passed[-1]["content"] == "[主 Agent 回答] 选 A"
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_sync_subagent_interaction.py::test_agent_runner_loop_resumed_messages_skips_construction -v 2>&1 | tail -10
```

Expected: FAIL（`agent_runner_loop` 没有 `resumed_messages` 参数）。

- [ ] **Step 3: 修改 _fifo_prune 加 is_resumed 参数**

`agent/generic/agent_loop.py:246-292` 的 `_fifo_prune` 改为：

```python
def _fifo_prune(messages, target_tokens, protect_recent_count=10, is_resumed=False):
    """FIFO 裁剪 messages 到 target_tokens。

    Args:
        messages: messages list（会被原地修改）
        target_tokens: 目标 token 数
        protect_recent_count: 保护最近 N 条消息不被裁剪（默认 10）
        is_resumed: 是否 resumed_messages 路径。True 时保护边界为
            messages[0]（system）+ 最近 protect_recent_count 条；
            False 时保持现有行为（保护 messages[0]+messages[1]）
    """
    if len(messages) <= 2:
        return 0
    # 计算保护边界
    if is_resumed:
        protect_end = max(2, len(messages) - protect_recent_count)
    else:
        protect_end = 2
    # 从 protect_end 之前删除（FIFO）—— 现有删除逻辑保留
    # ... 现有 L255-292 删除逻辑，把硬编码的 2 改为 protect_end ...
```

注：现有删除逻辑里 `i = 2` 等硬编码改为 `i = protect_end`，其余保留。

- [ ] **Step 4: 修改 agent_runner_loop 签名加 resumed_messages**

`agent/generic/agent_loop.py:391-410` 的 `agent_runner_loop` 签名加参数：

```python
def agent_runner_loop(
    client,
    system_prompt,
    system_message=None,
    user_input="",
    handler=None,
    tools_schema=None,
    max_turns=20,
    initial_user_content=None,
    context_window_tokens=128000,
    context_fifo_threshold=96000,
    context_target_threshold=38400,
    history=None,
    memory_context=None,
    supplement_drain=None,
    on_turn_end=None,
    resumed_messages=None,  # 新增
):
```

- [ ] **Step 5: 修改 messages 构造逻辑**

`agent/generic/agent_loop.py:416-477` 的 messages 构造加分支：

```python
if resumed_messages is not None:
    # 回复路径：直接用挂起的 messages，跳过 system_message + history + user_input 构造
    messages = resumed_messages
else:
    messages = [system_message]
    # ... 现有 L416-477 history 处理 + user_input append ...

# === L482+ 初始化保留在 if/else 之外（对所有路径执行） ===
turn = 0
last_prompt_tokens = 0
handler._last_prompt_tokens = 0
_compress_cooldown = False
handler._done_hooks = []
handler.max_turns = max_turns
_harness_fail_count = 0
warning_threshold = _read_warning_threshold()
yield StreamEvent("system", "chat_busy")
```

- [ ] **Step 6: 修改 _fifo_prune 调用点传 is_resumed**

`agent_loop.py` 内 `_fifo_prune` 的所有调用点（grep `_fifo_prune(`），加 `is_resumed=(resumed_messages is not None)`：

```python
_fifo_prune(messages, target_tokens, is_resumed=(resumed_messages is not None))
```

- [ ] **Step 7: 跑测试确认 PASS**

```bash
python -m pytest tests/test_sync_subagent_interaction.py::test_agent_runner_loop_resumed_messages_skips_construction -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 8: 跑全量测试确认无回归**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 9: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_sync_subagent_interaction.py
git commit -m "feat(agent_loop): agent_runner_loop resumed_messages 参数 + _fifo_prune is_resumed

回复路径跳过 messages 构造直接用挂起的 messages；_fifo_prune 在
resumed_messages 路径下用 protect_recent_count 保护边界而非硬编码
messages[1]。"
```

---

## Task 7: _run_agent_loop resumed_messages 参数

**Files:**
- Modify: `agent/subagent.py:189-246`

- [ ] **Step 1: 修改 _run_agent_loop 签名 + 透传**

`agent/subagent.py:189-204` 签名加：

```python
def _run_agent_loop(
    client,
    system_prompt,
    system_message=None,
    user_input="",
    handler=None,
    tools_schema=None,
    max_turns=20,
    initial_user_content=None,
    context_window_tokens=128000,
    context_fifo_threshold=96000,
    context_target_threshold=38400,
    history=None,
    memory_context=None,
    supplement_queue=None,
    resumed_messages=None,  # 新增
) -> tuple:
```

L228-246 调 `agent_runner_loop` 时透传：

```python
gen = agent_runner_loop(
    client=client,
    system_prompt=system_prompt,
    system_message=system_message,
    user_input=user_input,
    handler=handler,
    tools_schema=tools_schema,
    max_turns=max_turns,
    initial_user_content=initial_user_content,
    context_window_tokens=context_window_tokens,
    context_fifo_threshold=context_fifo_threshold,
    context_target_threshold=context_target_threshold,
    history=history,
    memory_context=memory_context,
    supplement_drain=supplement_queue.drain if supplement_queue is not None else None,
    resumed_messages=resumed_messages,  # 新增
)
```

- [ ] **Step 2: 跑全量测试确认无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 3: Commit**

```bash
git add agent/subagent.py
git commit -m "feat(subagent): _run_agent_loop resumed_messages 参数透传"
```

---

## Task 8: call_subagent 第三分支 + 同步新任务分支设 _is_sync_subagent + finally 条件化 + 后处理存挂起 + 顶部校验

**Files:**
- Modify: `agent/subagent.py:573-760`（call_subagent）+ L270-295（_extract_result_from_return_value control_flow_results）
- Test: `tests/test_sync_subagent_interaction.py`

- [ ] **Step 1: 写失败测试——call_subagent 三路入口**

在 `tests/test_sync_subagent_interaction.py` 加测试：

```python
def test_call_subagent_top_validation_no_task_no_answer():
    """call_subagent 顶部校验：无 task + 无 answer → 返回错误文本"""
    from agent import subagent
    result = subagent.call_subagent(
        agent_name="file-processor",
        task="",
        llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
    )
    assert "[错误]" in result
    assert "必须传 task" in result or "answer" in result
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_sync_subagent_interaction.py::test_call_subagent_top_validation_no_task_no_answer -v 2>&1 | tail -10
```

Expected: FAIL（顶部校验未加）。

- [ ] **Step 3: 修改 call_subagent 签名加 answer + answer_unique_name**

`agent/subagent.py:573-584` 加参数：

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
    answer: Optional[str] = None,              # 新增
    answer_unique_name: Optional[str] = None,  # 新增
) -> str:
```

- [ ] **Step 4: 修改 call_subagent 顶部加校验**

L606 `get_subagent_config` 之前加：

```python
def call_subagent(...):
    # 顶部校验：在 get_subagent_config 之前
    if not task and not answer:
        return "[错误] chat-with-xxx 必须传 task（新任务）或 answer + unique_name（回复子 Agent 问题）"

    # 1. 获取子 Agent 提示词 + temperature
    agent_config = get_subagent_config(agent_name)
    ...
```

- [ ] **Step 5: 修改同步新任务分支设 _is_sync_subagent=True**

`subagent.py:696-723` 的同步新任务分支（else 分支），在 L643 创建 handler 之后加：

```python
handler._is_sync_subagent = True  # 同步路径标记
```

异步新任务分支（L671-695）也加：

```python
handler._is_sync_subagent = False  # 异步路径标记
```

- [ ] **Step 6: 修改同步新任务分支 finally 条件化 + 后处理存挂起**

`subagent.py:696-723` 改为（见 spec §5.4）：

```python
else:
    # 同步路径：现有逻辑
    if supplement_queue is None:
        supplement_queue = SubagentSupplementQueue(unique_name="")
    unique_name = SubagentRegistry.register(agent_name, supplement_queue)
    supplement_queue.unique_name = unique_name
    handler._subagent_unique_name = unique_name
    handler._is_sync_subagent = True
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

- [ ] **Step 7: 加第三分支（回复路径）**

在 `subagent.py:671` 的 `if unique_name is not None:` 之前加第三分支（见 spec §5.2）：

```python
if answer is not None and answer_unique_name is not None:
    # 第三分支：回复路径
    instance = SubagentRegistry.get(answer_unique_name)
    if instance is None or getattr(instance, "state", None) != "waiting_for_answer":
        return f"[错误] 找不到挂起的子 Agent session（unique_name={answer_unique_name}），可能已被终止"
    if instance.agent_type != agent_name:
        return f"[错误] unique_name={answer_unique_name} 不属于子 Agent {agent_name}（实际属于 {instance.agent_type}），请检查 unique_name 是否传错"

    reply_text = _strip_at_prefix(answer, answer_unique_name)

    suspended_messages = instance.suspended_messages
    suspended_messages.append({"role": "user", "content": f"[主 Agent 回答] {reply_text}"})

    instance.state = "running"
    # 注释：不预检查 supplement_queue 是否已有 /stop，依赖 agent_runner_loop 内部 drain 检测

    try:
        result_text, return_value = _run_agent_loop(
            client=instance.suspended_client,
            system_prompt="",
            system_message=instance.suspended_system_message,
            user_input="",
            initial_user_content=None,
            handler=instance.suspended_handler,
            tools_schema=instance.suspended_tools_schema,
            memory_context=None,
            resumed_messages=suspended_messages,
            supplement_queue=instance.supplement_queue,
        )
        _maybe_suspend_session(
            unique_name=answer_unique_name,
            return_value=return_value,
            handler=instance.suspended_handler,
            client=instance.suspended_client,
            tools_schema=instance.suspended_tools_schema,
            system_message=instance.suspended_system_message,
        )
    finally:
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
    # 同步新任务分支（改动见 Step 6）
    ...
```

- [ ] **Step 8: 实现 _strip_at_prefix helper**

在 `agent/subagent.py` 加：

```python
def _strip_at_prefix(answer: str, unique_name: str) -> str:
    """剥除 answer 的 '@unique_name ' 前缀。找不到前缀原样使用，记 warning。"""
    import re
    pattern = rf"^@{re.escape(unique_name)}\s+"
    match = re.match(pattern, answer)
    if match:
        return answer[match.end():]
    logger.warning(f"[StripAtPrefix] answer 不含 @{unique_name} 前缀，原样使用: {answer[:100]}")
    return answer
```

- [ ] **Step 9: 实现 _maybe_suspend_session helper**

在 `agent/subagent.py` 加（见 spec §5.5）：

```python
def _maybe_suspend_session(unique_name, return_value, handler, client, tools_schema, system_message):
    """检测同步 @niu-agent 挂起信号，存挂起状态到 registry。必须在 try 块内、finally 之前调用。"""
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

- [ ] **Step 10: 修改 control_flow_results 集合**

`agent/subagent.py:285` 改为：

```python
control_flow_results = {
    "CONTEXT_OVERFLOW", "EXITED", "MAX_TURNS_EXCEEDED", "CURRENT_TASK_DONE", "TERMINATED_BY_SUPPLEMENT",
    "STOPPED",           # 顺便补：子 Agent 收到 /stop 终止（之前漏在集合外）
    "INTERCEPTED_SYNC",  # 新增：同步 @niu-agent 挂起
}
```

注：EXITED 已存在，保留不动。

- [ ] **Step 11: 跑测试确认 PASS**

```bash
python -m pytest tests/test_sync_subagent_interaction.py -v 2>&1 | tail -20
```

Expected: 顶部校验测试 PASS。加更多测试覆盖第三分支（见下一步）。

- [ ] **Step 12: 写测试——第三分支 agent_type 不匹配**

```python
def test_call_subagent_third_branch_agent_type_mismatch(monkeypatch):
    """第三分支 agent_type 不匹配 → 返回错误文本"""
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry, RunningSubagent
    from agent.subagent_supplement import SubagentSupplementQueue

    # 注册一个 agent_type="A" 的 session
    sq = SubagentSupplementQueue(unique_name="")
    unique_name = SubagentRegistry.register("A", sq)
    sq.unique_name = unique_name
    instance = SubagentRegistry.get(unique_name)
    instance.state = "waiting_for_answer"

    # 用 agent_name="B" 调第三分支
    result = subagent.call_subagent(
        agent_name="B",
        task="",
        llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        answer="@xxx 回答",
        answer_unique_name=unique_name,
    )

    SubagentRegistry.unregister(unique_name)  # 清理
    assert "[错误]" in result
    assert "不属于" in result
```

- [ ] **Step 13: 跑测试确认 PASS**

```bash
python -m pytest tests/test_sync_subagent_interaction.py::test_call_subagent_third_branch_agent_type_mismatch -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 14: 跑全量测试确认无回归**

```bash
python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 15: Commit**

```bash
git add agent/subagent.py tests/test_sync_subagent_interaction.py
git commit -m "feat(subagent): call_subagent 第三分支 + 同步新任务分支 _is_sync_subagent + finally 条件化 + _maybe_suspend_session + control_flow_results 集合更新

- 第三分支（回复路径）：从 registry 拿回挂起 session，agent_type 校验
- 同步新任务分支设 handler._is_sync_subagent=True
- finally 条件化 unregister（state=waiting_for_answer 跳过）
- _maybe_suspend_session helper（异常安全 try/except + fallback）
- _strip_at_prefix helper
- control_flow_results 集合加 STOPPED + INTERCEPTED_SYNC
- 顶部校验：无 task + 无 answer 返回错误文本"
```

---

## Task 9: chat-with-xxx schema 改动 + _call_subagent_gen 透传

**Files:**
- Modify: `agent/runner.py:312-393`（chat-with-xxx schema）+ `agent/handler.py:939-1059`（_call_subagent_gen）

- [ ] **Step 1: 修改 chat-with-xxx schema 加可选参数**

`agent/runner.py:312-393` 的 chat-with-xxx schema 生成处，每个 schema 加 `answer` + `unique_name` 可选参数 + `task` 改 optional：

```python
{
    "name": f"chat-with-{name}",
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务描述（回复路径可传空字符串）"},
            "answer": {"type": "string", "description": "回复子 Agent 的 @niu-agent 问题（含 @子名 前缀）"},
            "unique_name": {"type": "string", "description": "子 Agent 唯一名（回复时必填）"},
            "async_mode": {"type": "boolean", ...}  # 已有，allowAsync 时才有
        },
        "required": []  # task 改 optional
    }
}
```

- [ ] **Step 2: 修改 _call_subagent_gen 解析参数 + 透传**

`agent/handler.py:943-944` 参数解析扩展：

```python
task = args.get("task", "")
async_mode = args.get("async_mode", False)
answer = args.get("answer")  # 新增
unique_name_arg = args.get("unique_name")  # 新增
```

L998 调 call_subagent 透传（完整参数清单）：

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

- [ ] **Step 3: 跑全量测试确认无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 4: Commit**

```bash
git add agent/runner.py agent/handler.py
git commit -m "feat(runner/handler): chat-with-xxx schema 加 answer+unique_name + _call_subagent_gen 透传"
```

---

## Task 10: `call_subagent_with_auto_answer` helper 实现

**Files:**
- Modify: `agent/subagent.py`（加 helper）+ 新建 `tests/test_call_subagent_with_auto_answer.py`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_call_subagent_with_auto_answer.py`：

```python
"""call_subagent_with_auto_answer helper 单元测试"""
from unittest import mock


def test_helper_returns_directly_for_non_at_niu_result():
    """第一次返回非 @niu-agent 文本 → 直接返回"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent", return_value="任务完成结果"):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"


def test_helper_auto_replies_to_at_niu_question():
    """第一次返回 @niu-agent 问题 → 自动回复 → 第二次返回 @end → 返回最终结果"""
    from agent import subagent

    call_count = [0]
    def mock_call_subagent(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "[file-processor-a1b2] 我该选哪个？"
        else:
            return "任务完成结果"

    with mock.patch.object(subagent, "call_subagent", side_effect=mock_call_subagent):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "任务完成结果"
    assert call_count[0] == 2


def test_helper_does_not_misidentify_normal_result():
    """子 Agent 正常结果含 [已完成] 不被误判为 @niu-agent 问题"""
    from agent import subagent

    with mock.patch.object(subagent, "call_subagent", return_value="[已完成] 文件 X 处理完毕"):
        result = subagent.call_subagent_with_auto_answer(
            agent_name="file-processor",
            task="做 X",
            llm_config={"model": "test", "api_key": "test", "base_url": "http://localhost"},
        )
    assert result == "[已完成] 文件 X 处理完毕"
```

- [ ] **Step 2: 跑测试确认 FAIL**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/test_call_subagent_with_auto_answer.py -v 2>&1 | tail -10
```

Expected: FAIL with `AttributeError: module 'agent.subagent' has no attribute 'call_subagent_with_auto_answer'`。

- [ ] **Step 3: 实现 helper**

在 `agent/subagent.py` 加：

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

- [ ] **Step 4: 跑测试确认 PASS**

```bash
python -m pytest tests/test_call_subagent_with_auto_answer.py -v 2>&1 | tail -10
```

Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add agent/subagent.py tests/test_call_subagent_with_auto_answer.py
git commit -m "feat(subagent): call_subagent_with_auto_answer helper

程序触发子 Agent 专用，自动回复 @niu-agent 问题。严格正则匹配
unique_name 格式，避免误判正常结果。"
```

---

## Task 11: 派 Agent 全面排查程序触发点 + 替换为 helper

**Files:**
- Modify: `niu_api/compat.py:1861/1935/2006/2174/2397/2562/2635/2706/2810` + `agent/runner.py:1223`

- [ ] **Step 1: 派 Agent 全面排查所有直接调 call_subagent 的位置**

派 Agent grep 全仓 `call_subagent(` 调用点，区分：
- 主 Agent 工具调用路径（`handler.py:998`，不改）
- 程序触发路径（替换为 `call_subagent_with_auto_answer`）

- [ ] **Step 2: 替换 niu_api/compat.py 9 处**

把 `compat.py:1861/1935/2006/2174/2397/2562/2635/2706/2810` 的 `call_subagent(...)` 替换为 `call_subagent_with_auto_answer(...)`。需要先 `from agent.subagent import call_subagent_with_auto_answer`。

- [ ] **Step 3: 替换 agent/runner.py:1223**

把 `runner.py:1223` 的 `call_subagent(...)` 替换为 `call_subagent_with_auto_answer(...)`。

- [ ] **Step 4: 跑全量测试确认无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add niu_api/compat.py agent/runner.py
git commit -m "refactor(compat/runner): 程序触发子 Agent 调用替换为 call_subagent_with_auto_answer

10 处程序触发点（auto_tidy + force 压缩 + 手动 tidy API）替换为
helper，自动回复 @niu-agent 问题。"
```

---

## Task 12: request_stop_all_subagents 改造 + 主 Agent 工具循环退出清理

**Files:**
- Modify: `agent/runner.py:54-71`（request_stop_all_subagents）+ 主 Agent 工具循环 finally 块

- [ ] **Step 1: 修改 request_stop_all_subagents 加挂起 session 扫描**

`agent/runner.py:54-71` 改为：

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

- [ ] **Step 2: 加 cleanup_suspended_sync_subagents helper**

在 `agent/runner.py` 加：

```python
def cleanup_suspended_sync_subagents():
    """主 Agent 工具循环退出时清理所有挂起的同步子 Agent session。"""
    for instance in SubagentRegistry.list_running():
        state = getattr(instance, "state", "running")
        is_sync = getattr(instance, "is_sync", False)
        if state == "waiting_for_answer" and is_sync:
            SubagentRegistry.unregister(instance.unique_name)
            logger.info(f"[CleanupSuspendedSync] 已清理挂起同步子 Agent: {instance.unique_name}")
```

- [ ] **Step 3: 在主 Agent 工具循环 finally 块调 cleanup**

`agent/runner.py` 主 Agent 工具循环生成器（L2184-2201 附近）的 finally 块加：

```python
finally:
    cleanup_suspended_sync_subagents()
    # ... 现有清理逻辑 ...
```

实施时 grep `finally` 在 runner.py 主 Agent 路径定位。

- [ ] **Step 4: 跑全量测试确认无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/agent && python -m pytest tests/ -v 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py
git commit -m "feat(runner): request_stop_all_subagents 加挂起 session 扫描 + cleanup_suspended_sync_subagents

/stop 时直接 unregister 挂起同步 session（无活跃 agent_runner_loop
消费 supplement）；主 Agent 工具循环退出时清理残留挂起 session。"
```

---

## Task 13: §9B niu.md / agent-template.md 提示词增量

**Files:**
- Modify: `config/agents/niu.md` L255 附近 + `config/agent-template.md` L27 附近

- [ ] **Step 1: niu.md 加同步子 Agent @niu-agent 问题处理提示词**

在 `config/agents/niu.md` 的"### 收到 [子名] 问题消息时"段（L255 附近）追加（见 spec §9B.2 完整文案）：

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

- [ ] **Step 2: agent-template.md 加增量**

在 `config/agent-template.md` 的"## 提示词正文"段（L27 附近）追加：

```markdown
- **何时主动询问主 Agent**：所有子 Agent（同步 + 异步）都被程序注入 @niu-agent/@end 守则。子 Agent 用 `@niu-agent ` 前缀询问主 Agent，用 `@end ` 前缀结束会话。子 Agent 不需要在输出里包含自己的标识符，程序会自动在你的问题前加上唯一标识，主 Agent 据此回复你。
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/niu.md config/agent-template.md
git commit -m "docs(prompt): §9B niu.md + agent-template.md 提示词增量

同步子 Agent 交互关键依赖——主 Agent LLM 看到 JSON 工具结果含
[子名] 问题时调 chat-with-xxx(task='', answer, unique_name) 回复。"
```

---

## Task 14: 文档同步

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md` + `docs/manual-general-subagent.md`

- [ ] **Step 1: 更新 docs/SYSTEM_MANUAL.md**

加同步子 Agent 交互通道描述（参考 spec §3 数据流）。

- [ ] **Step 2: 更新 docs/manual-general-subagent.md**

加同步子 Agent @niu-agent/@end 交互说明。

- [ ] **Step 3: Commit**

```bash
git add docs/SYSTEM_MANUAL.md docs/manual-general-subagent.md
git commit -m "docs: 同步子 Agent 交互通道文档更新"
```

---

## Task 15: 端到端测试 + 知识图谱回归验证

**Files:**
- Test: 真实 LLM 端到端测试（spec §11.2 的 8 个场景）

- [ ] **Step 1: 启动程序**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && ./niu &
sleep 10 && ps aux | grep -i niu | head -5
```

Expected: 进程启动无错误。

- [ ] **Step 2: 端到端测试 1——同步子 Agent @niu-agent 询问 + 主 Agent 回复 + 子 Agent 继续**

通过前端调主 Agent 创建一个会问澄清问题的同步子 Agent（如 photo-organizer），观察：
- 主 Agent 调 chat-with-xxx → 子 Agent @niu-agent 问澄清问题
- 主 Agent 看到 JSON 工具结果含 `[子名] 问题`
- 主 Agent 回 `@子名 回答`
- 子 Agent 收到回答继续工作 → @end 返回结果

验证日志：
```bash
grep -E "chat-with-|@niu-agent|INTERCEPTED_SYNC" REDACTED_USER_PATH/tools/ai-bot/logs/*.log | tail -30
```

Expected: 日志显示两次 chat-with-xxx 调用 + INTERCEPTED_SYNC 拦截。

- [ ] **Step 3: 端到端测试 2——同步子 Agent 多轮 @niu-agent**

构造场景让子 Agent 连续问 3 次 @niu-agent，验证主 Agent 回复 3 次后子 Agent @end。

- [ ] **Step 4: 端到端测试 5——程序触发子 Agent @niu-agent 自动回复**

触发 auto_tidy（如让主 Agent 上下文超阈值），观察 entity-extractor / dream-evolver 子 Agent 是否能 @niu-agent 自动回复。

- [ ] **Step 5: 端到端测试 6——/stop 终止挂起的同步子 Agent**

子 Agent @niu-agent 挂起后按 /stop，验证 registry 无残留。

- [ ] **Step 6: 端到端测试 7——异步路径回归**

构造异步子 Agent 场景，验证 5 次 @niu-agent + @end + 格式错误 + /stop 全部正常。

- [ ] **Step 7: 端到端测试 8——主 Agent LLM 真实纠错行为**

构造一个会让主 Agent LLM 误用 task 字段塞回答的场景，验证 LLM 能从错误文本中纠正为正确格式。

- [ ] **Step 8: 知识图谱回归验证**

让 entity-extractor 处理含 `@niu-agent` 的对话，查 LightRAG 数据库（`~/.niu/lightrag/`）确认无新边连到根节点 `niu`：

```bash
# 查 LightRAG 数据库（具体查询命令视实现而定）
python -c "
from agent.tool_registry import get_registry
r = get_registry()
# 查根节点 niu 的边
..."
```

Expected: 无新边连到根节点 `niu`。

- [ ] **Step 9: 杀进程清理**

```bash
pkill -f "niu" 2>/dev/null
# 注意：不能用 pkill -9，避免 LightRAG vdb 文件损坏
```

- [ ] **Step 10: Commit 测试日志/记录（如有）**

```bash
# 如有测试记录文件
git add docs/test-records/ 2>/dev/null
git commit -m "test(stage4): 端到端测试通过 + 知识图谱回归验证" 2>/dev/null
```

---

## Task 16: 全量代码审查

- [ ] **Step 1: 派 code-reviewer Agent 做全量代码审查**

派 Agent 审查所有改动（agent_loop.py / subagent.py / subagent_registry.py / handler.py / runner.py / compat.py + 测试 + 提示词 + 文档），重点：
- spec 合规性（对照 spec v14）
- 代码质量（命名 / 异常处理 / 资源管理）
- 测试覆盖（单元 + 端到端）
- 回归风险（异步路径 / 主 Agent 路径）

- [ ] **Step 2: 修复审查发现的问题**

按审查报告修复 BLOCKER + 关键 IMPORTANT。

- [ ] **Step 3: 最终 Commit + Push**

```bash
git add -A
git commit -m "fix(stage4): 全量代码审查后修复"
git push niu-agent main
```

---

## 自检清单

- [ ] spec §9A 全仓改名完成（grep 无旧 `@niu` 残留）
- [ ] SubagentRegistry 6 字段扩展
- [ ] 守则注入恢复（所有子 Agent 统一注入）
- [ ] 拦截层 tuple 返回 + INTERCEPTED_SYNC 分支
- [ ] _ask_main_agent_impl_sync 实现
- [ ] agent_runner_loop resumed_messages 参数 + _fifo_prune is_resumed
- [ ] _run_agent_loop resumed_messages 透传
- [ ] call_subagent 三分支 + finally 条件化 + _maybe_suspend_session + control_flow_results 集合
- [ ] chat-with-xxx schema + _call_subagent_gen 透传
- [ ] call_subagent_with_auto_answer helper
- [ ] 程序触发点替换（10 处）
- [ ] request_stop_all_subagents + cleanup_suspended_sync_subagents
- [ ] niu.md / agent-template.md 提示词增量
- [ ] 文档同步
- [ ] 端到端测试通过（8 个场景）
- [ ] 知识图谱回归验证通过
- [ ] 全量代码审查通过
