# context-manager 绕过 @niu-agent/@end 守则与拦截层 Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复同步异步子 Agent 调用改造（阶段一/二/三/四）引入的 context-manager 失效 bug。改造给所有子 Agent 强制注入 `@niu-agent`/`@end` 守则并强制拦截无 `@` 前缀的输出，导致 context-manager 的原生 `keep=/update=/cursor=` 输出被误判为 FORMAT_ERROR，第二轮 LLM 困惑后放弃输出，runner.py 抛 `ValueError("No keep= line found")`，压缩完全失败。**修复方向**：让 context-manager 不被注入守则、输出不被拦截，原有 `keep=/update=/cursor=` 解析写 messages 数据库的逻辑完全不动。

**Architecture:** 在两个改造引入的代码点加 `agent_name == "context-manager"` 判断绕过：(1) `agent/subagent.py` L501-503 守则注入处，context-manager 不注入 `_SUBAGENT_ASK_GUIDE_TEMPLATE`；(2) `agent/generic/agent_loop.py` `_intercept_at_prefix_content` 入口处，context-manager 直接返回 `NO_INTERCEPTION`，不走 FORMAT_ERROR 兜底。判断方式用 `agent_name` 显式比对，因为 `call_subagent_with_auto_answer` 没有携带"程序触发"标记、handler 上也没有可靠字段，最直接最不易误伤。原有 `runner.py` L1310-1337 和 `compat.py` L2216-2234 / L2851-2876 的 `keep=/update=/cursor=` 解析逻辑完全不动。

**Tech Stack:** Python 3.11+，pytest，subagent 同步调用架构

---

## Context

### 当前 bug

定时任务触发后主 Agent 上下文 ~84% token，触发强制压缩（模式三，runner.py 的 `_on_context_high_usage` 回调）：

1. 压缩第一轮：context-manager 输出正确的 `keep=/update=/cursor=` 格式（这是它原本就该输出的格式，程序按这个格式直接写 messages 数据库）
2. 改造后的拦截层把这套输出当成"格式错误"（无 `@` 前缀无 tool_calls），append `[对话格式错误]` 提示词让 LLM 重跑
3. 压缩第二轮：context-manager 收到格式错误提示词，困惑后输出 `@end 压缩决策已完成` 放弃
4. runner.py 解析时找不到 `keep=` 行，抛 `ValueError("No keep= line found in sub-agent reply")`，压缩完全没执行
5. 压缩失败后 messages 状态异常，主 Agent 最终请求里含 24 条 `role: subagent_msg` 消息，LLM 拒绝（只支持 system/assistant/user/tool），定时任务彻底失败

### 根因（两个错误，都是本次改造引入的）

**错误一：动态注入了不该注入的提示词给 context-manager。**
- 改造 commit `2a10d754 feat(subagent): 恢复守则注入，所有子 Agent 统一注入 @niu-agent/@end` 在 `agent/subagent.py` L501-503 加了"所有子 Agent 统一注入"逻辑
- 但 context-manager 的原设计是直接输出 `keep=/update=/cursor=` 让程序写数据库，根本不需要 `@niu-agent`/`@end` 守则
- 这套注入污染了 context-manager 的系统提示词，让 LLM 困惑"我到底该输出 `keep=` 还是 `@end`"

**错误二：拦截了 context-manager 的输出，按新逻辑检查。**
- 改造 commit `f75bb940 feat(agent_loop): @前缀子Agent意图识别拦截层` 在 `agent/generic/agent_loop.py` 加了 `_intercept_at_prefix_content` 函数
- agent_runner_loop 在 L664-697 每个 LLM 响应后必经拦截层，无 `@` 前缀无 tool_calls 就 FORMAT_ERROR
- 这套拦截强加给 context-manager，导致它的正确 `keep=/update=/cursor=` 输出被误判为格式错误

### 改造前基线（已确认）

- **基线 SHA**：`66f8ed76 feat(ui): 压缩时圆环 SVG 旋转动画` — 阶段一 spec `80a025e7` 之前的最后一个正常代码 commit
- 已验证：基线的 `agent/subagent.py`、`agent/generic/agent_loop.py`、`config/agents/context-manager.md` **均无** `@niu`/`@end`/`守则`/`_intercept_at_prefix_content`/`FORMAT_ERROR` 字样
- 当时的 context-manager 正常工作：系统提示词里没有守则、agent_runner_loop 不拦截，LLM 直接输出 `keep=/update=/cursor=`，runner.py 解析写库

### 三个关键代码点（当前 HEAD）

| 文件 | 行号 | 内容 | 改动 |
|------|------|------|------|
| `agent/subagent.py` | L501-503 | `# 4. 强制注入 @niu-agent/@end 守则（所有子 Agent）` + `if _SUBAGENT_ASK_GUIDE_MARKER not in static_system: static_system += ...` | **加 `agent_name != "context-manager"` 判断绕过** |
| `agent/generic/agent_loop.py` | L81-94（`_intercept_at_prefix_content` 入口） | `is_sync_subagent = getattr(handler, "_is_sync_subagent", False)`；主 Agent 分支 `if memory_context is None and not is_sync_subagent` | **新增 context-manager 入口绕过：在 is_sync_subagent 计算后加一句 `if <context-manager 判断>: return (NO_INTERCEPTION, None)`** |
| `agent/runner.py` | L1310-1337 | 解析 `keep=/update=/cursor=` 写 messages 数据库 | **不动** |
| `niu_api/compat.py` | L2216-2234（模式二）/ L2851-2876（模式三 force） | 解析 `keep=/update=` / `keep=/update=/cursor=` | **不动** |

### 判断 context-manager 的方式（设计选择）

**选用 `agent_name == "context-manager"` 字符串比对**，理由：

1. **最直接**：call_subagent 入口 `agent_name` 就是函数参数，直接可用
2. **最不易误伤**：context-manager 是配置文件名（`config/agents/context-manager.md`）锁定的，跟改造引入的 `@niu-agent` 守则语义正交——其他子 Agent（file-processor/dream-evolver/entity-extractor/journal-agent/event-manager/browser-operator/...）仍该有守则、该被拦截
3. **不依赖运行时字段**：handler 上有 `_subagent_unique_name` 和 `_is_sync_subagent` 两个改造引入的字段，但都不区分 context-manager vs 其他子 Agent。call_subagent_with_auto_answer 没有携带"程序触发"标记
4. **未来扩展**：如果将来有别的子 Agent 也是"程序触发、直接输出格式化文本"（如 entity-extractor 输出 JSON），可以加同样的判断（如 `agent_name in {"context-manager", "entity-extractor"}`）；但**本次只改 context-manager，最小化**

### 关键约束（用户铁律）

- **修改前必须先做临时提交备份**（铁律 #3）— Task 0 做备份
- **禁止 `git reset --hard` / force push**（铁律 #9）
- **测试必须用真实数据 + 真实 LLM**（铁律 #5）— Task 3 用真实定时任务触发，不 mock
- **修改前必须用 gitnexus 分析影响范围**（铁律 #4）— 本计划只改注入点和拦截点，blast radius 极小，可省略（但子 Agent 执行时仍要跑）
- **派出去的子 Agent 必须遵守所有铁律**

### 关键代码位置（HEAD = 27b287f4）

**注入点** `agent/subagent.py` L485-510：
```python
def build_subagent_system_segments(agent_name: str) -> tuple:
    static_system = get_subagent_prompt(agent_name)
    # 1. 注入用户信息（L491-493）
    # 2. 注入职责边界段（L498-499）
    # 3. 强制注入 @niu-agent/@end 守则（所有子 Agent）  ← L501-503，本计划改这里
    if _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
        static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
    # 5. 动态段 Current Time（L506-508）
```

**拦截点** `agent/generic/agent_loop.py` L56-144 `_intercept_at_prefix_content`：
```python
def _intercept_at_prefix_content(content, tool_calls, messages, handler, memory_context):
    is_sync_subagent = getattr(handler, "_is_sync_subagent", False)  # L81
    if tool_calls:
        return (NO_INTERCEPTION, None)  # L83-84
    stripped = (content or "").lstrip()  # L86
    if memory_context is None and not is_sync_subagent:  # L91 主 Agent 分支
        if _check_main_agent_content_reply_to_suspended(stripped, messages):
            return (FORMAT_ERROR, None)
        return (NO_INTERCEPTION, None)
    # L97-144 子 Agent 拦截分支（@niu-agent / @end / FORMAT_ERROR）
    # ← context-manager 同步调用 (_is_sync_subagent=True, memory_context=None)
    #   会落到这里，第一轮 keep=... 输出被 L143 误判 FORMAT_ERROR
    #   本计划在 L81 后新增 context-manager 绕过判断
```

**原有 keep=/update=/cursor= 解析点** `agent/runner.py` L1310-1337：
```python
for line in result.splitlines():
    line = line.strip()
    if line.lower().startswith("keep="):
        keep_idxs = _parse_idx_list(line.split("=", 1)[1].strip())
    elif line.lower().startswith("update="):
        # 解析 update_list
    elif line.lower().startswith("cursor="):
        # 解析 cursor_idx
if not keep_idxs:
    raise ValueError("No keep= line found in sub-agent reply")
```
（`niu_api/compat.py` L2216-2234 / L2851-2876 有同构解析逻辑，都不动）

---

## File Structure

```
ai-bot/                              # 项目根
├── agent/
│   ├── subagent.py                  # 改 L501-503 注入点
│   └── generic/
│       └── agent_loop.py            # 改 L81 后加绕过分支
├── tests/
│   ├── test_at_prefix_interception.py        # 已有，回归验证
│   ├── test_context_manager_bypass_at_prefix.py  # 新增，TDD 失败测试
│   └── test_call_subagent_with_auto_answer.py # 已有，回归验证
├── config/agents/
│   └── context-manager.md           # 不改，验证基线无 @niu/@end
├── agent/runner.py                  # 不改，验证 keep=/update=/cursor= 仍工作
└── niu_api/compat.py                # 不改，验证模式二/三解析仍工作
```

---

## Tasks

### Task 0: 修改前临时备份提交

- [ ] **Step 0.1**：检查工作区干净（除本次新计划文件外）
```bash
cd <repo_root>
git status
```
- [ ] **Step 0.2**：临时备份提交（标注问题名+节点类型+基线 hash）
```bash
cd <repo_root>
git add -A
git commit -m "backup: context-manager 绕过 @niu-agent/@end 守则改造前临时备份 (baseline 27b287f4)

问题：context-manager 被 @niu-agent/@end 守则注入和拦截层误伤，
keep=/update=/cursor= 正确输出被 FORMAT_ERROR，压缩失败。

准备改两个点：
1. agent/subagent.py L501-503 注入点加 agent_name != 'context-manager' 绕过
2. agent/generic/agent_loop.py _intercept_at_prefix_content 入口加绕过

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1: TDD — 先写失败测试

**目标**：用 pytest 写 4 个测试覆盖 (1) 不注入守则 (2) 不拦截 (3) 一次性成功 (4) 其他子 Agent 仍受守则和拦截约束。

- [ ] **Step 1.1**：创建测试文件 `tests/test_context_manager_bypass_at_prefix.py`
```python
"""context-manager 绕过 @niu-agent/@end 守则注入和拦截层的单元测试。

背景：同步异步子 Agent 调用改造给所有子 Agent 强制注入守则 + 强制拦截无 @ 前缀的输出，
误伤了 context-manager 的原生 keep=/update=/cursor= 输出格式。本测试验证：
1. context-manager 系统提示词不含守则
2. context-manager 输出 keep=/update=/cursor= 不被拦截（返回 NO_INTERCEPTION）
3. 其他子 Agent（如 file-processor）仍该被注入守则、该被拦截
4. context-manager 同步调用（_is_sync_subagent=True, memory_context=None）不被拦截
"""
from unittest import mock


def test_context_manager_system_prompt_has_no_at_niu_guide():
    """context-manager 的系统提示词里不包含 @niu-agent/@end 守则段"""
    from agent.subagent import build_subagent_system_segments
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER

    static_system, _ = build_subagent_system_segments("context-manager")
    assert _SUBAGENT_ASK_GUIDE_MARKER not in static_system
    assert "@niu-agent" not in static_system
    assert "## 子 Agent 与主 Agent 对话规则" not in static_system


def test_file_processor_system_prompt_still_has_at_niu_guide():
    """file-processor 的系统提示词里仍包含守则段（验证绕过只针对 context-manager）"""
    from agent.subagent import build_subagent_system_segments
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER

    static_system, _ = build_subagent_system_segments("file-processor")
    assert _SUBAGENT_ASK_GUIDE_MARKER in static_system
    assert "@niu-agent" in static_system


def test_context_manager_keep_output_not_intercepted():
    """context-manager 输出 keep=/update=/cursor= 时，拦截层返回 NO_INTERCEPTION"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True  # 同步路径
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


def test_context_manager_bypass_doesnt_append_format_error():
    """context-manager 输出无 @ 前缀时，messages 不被追加 [对话格式错误] 提示"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True
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


def test_file_processor_still_intercepted_when_no_at_prefix():
    """file-processor 输出无 @ 前缀无 tool_calls 时，仍返回 FORMAT_ERROR（验证绕过只针对 context-manager）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "file-processor-a1b2"
    fake_handler._is_sync_subagent = False
    messages = [
        {"role": "system", "content": "你是 file-processor"},
        {"role": "user", "content": "处理文件"},
    ]
    content = "我处理完了"  # 无 @ 前缀

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),  # 异步路径
    )
    assert result == (agent_loop.FORMAT_ERROR, None)
```

- [ ] **Step 1.2**：跑测试确认失败（context-manager 当前会被注入守则、会被拦截）
```bash
cd <repo_root>
python -m pytest tests/test_context_manager_bypass_at_prefix.py -v 2>&1 | tail -40
```
**预期失败**：
- `test_context_manager_system_prompt_has_no_at_niu_guide` 失败（守则被注入了）
- `test_context_manager_keep_output_not_intercepted` 失败（被 FORMAT_ERROR 了）
- `test_context_manager_bypass_doesnt_append_format_error` 失败（messages 被追加了错误提示）
- `test_file_processor_*` 两个测试通过（其他子 Agent 仍正常受约束）

---

### Task 2: 改注入点（subagent.py）

**目标**：让 `build_subagent_system_segments("context-manager")` 不注入 `_SUBAGENT_ASK_GUIDE_TEMPLATE`。

- [ ] **Step 2.1**：编辑 `agent/subagent.py` L501-503
```python
# 改前（L501-503）：
    # 4. 强制注入 @niu-agent/@end 守则（所有子 Agent）
    if _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
        static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
```
改为：
```python
    # 4. 强制注入 @niu-agent/@end 守则
    # context-manager 例外：它原设计是直接输出 keep=/update=/cursor= 让程序写数据库，
    # 不走 @niu-agent/@end 交互通道。注入守则会污染它的输出格式，导致压缩失败。
    # 详见 docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
    if agent_name != "context-manager" and _SUBAGENT_ASK_GUIDE_MARKER not in static_system:
        static_system += "\n\n" + _SUBAGENT_ASK_GUIDE_TEMPLATE
```

- [ ] **Step 2.2**：Python 语法检查
```bash
cd <repo_root>
python -c "import agent.subagent; print('OK')"
```

- [ ] **Step 2.3**：跑 Task 1 的测试，验证注入侧两个测试通过
```bash
cd <repo_root>
python -m pytest tests/test_context_manager_bypass_at_prefix.py::test_context_manager_system_prompt_has_no_at_niu_guide tests/test_context_manager_bypass_at_prefix.py::test_file_processor_system_prompt_still_has_at_niu_guide -v
```
**预期**：两个测试通过。其他三个测试仍失败（拦截点还没改）。

---

### Task 3: 改拦截点（agent_loop.py）

**目标**：让 `_intercept_at_prefix_content` 对 context-manager 直接返回 `NO_INTERCEPTION`，不走 FORMAT_ERROR 兜底。

- [ ] **Step 3.1**：编辑 `agent/generic/agent_loop.py` `_intercept_at_prefix_content` 入口（L81 后）

当前代码（L81-94）：
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
```

改为（在 L81 后插入 context-manager 绕过判断）：
```python
    is_sync_subagent = getattr(handler, "_is_sync_subagent", False)
    # tool_calls 时不拦截（正常工具调用）
    if tool_calls:
        return (NO_INTERCEPTION, None)

    # context-manager 绕过：它原设计是直接输出 keep=/update=/cursor= 让程序写数据库，
    # 不走 @niu-agent/@end 交互通道。拦截会导致正确输出被 FORMAT_ERROR，压缩失败。
    # 详见 docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
    unique_name = getattr(handler, "_subagent_unique_name", "") or ""
    if unique_name == "context-manager":
        return (NO_INTERCEPTION, None)

    stripped = (content or "").lstrip()

    # 主 Agent 分支：检测 content 误回复同步挂起子 Agent
    # 主 Agent 特征：memory_context is None and not is_sync_subagent
    # 误回复模式：content 以 @<同步挂起子名> 开头但本轮没调 chat-with 工具
    if memory_context is None and not is_sync_subagent:
        if _check_main_agent_content_reply_to_suspended(stripped, messages):
            return (FORMAT_ERROR, None)
        return (NO_INTERCEPTION, None)
```

**为什么用 `_subagent_unique_name` 而不是新增参数**：
- `_intercept_at_prefix_content` 当前签名 `(content, tool_calls, messages, handler, memory_context)` 没有显式 agent_name 参数
- handler 上有改造引入的 `_subagent_unique_name` 字段，subagent.py L848/L850/L883/L884 设置：
  - 同步路径：`unique_name = agent_name`（即 "context-manager"）
  - 异步路径：`unique_name = agent_name + "-" + hex`（如 "file-processor-a1b2"）
- 同步调用 context-manager 时，`_subagent_unique_name == "context-manager"`，精确匹配
- 异步调用 context-manager（如果将来有的话）`_subagent_unique_name == "context-manager-xxxx"`，**不匹配**——但 context-manager 当前是同步调用的（`call_subagent_with_auto_answer` 同步路径），不存在异步调用，所以不影响

- [ ] **Step 3.2**：Python 语法检查
```bash
cd <repo_root>
python -c "from agent.generic import agent_loop; print('OK')"
```

- [ ] **Step 3.3**：跑 Task 1 全部测试，验证 5 个测试全部通过
```bash
cd <repo_root>
python -m pytest tests/test_context_manager_bypass_at_prefix.py -v
```
**预期**：5 个测试全部通过。

---

### Task 4: 回归测试 — 现有测试不破坏

**目标**：跑现有相关测试，确认改动不破坏其他子 Agent（file-processor/dream-evolver 等仍受守则约束、仍被拦截）。

- [ ] **Step 4.1**：跑 at-prefix 拦截层测试
```bash
cd <repo_root>
python -m pytest tests/test_at_prefix_interception.py -v 2>&1 | tail -40
```
**预期**：全部通过。重点验证：
- `test_no_at_prefix_no_tool_calls_returns_format_error` 仍通过（其他子 Agent 无 @ 前缀仍 FORMAT_ERROR）
- `test_main_agent_not_intercepted` 仍通过（主 Agent 不被拦截）
- `test_sync_subagent_at_niu_returns_intercepted_sync` 仍通过（其他同步子 Agent 输出 @niu-agent 仍走 INTERCEPTED_SYNC）

- [ ] **Step 4.2**：跑通用子 Agent 测试
```bash
cd <repo_root>
python -m pytest tests/test_general_subagent.py tests/test_call_subagent_with_auto_answer.py -v 2>&1 | tail -40
```
**预期**：全部通过。

- [ ] **Step 4.3**：跑 subagent 相关全套测试
```bash
cd <repo_root>
python -m pytest tests/test_subagent_registry.py tests/test_subagent_supplement.py tests/test_subagent_supplement_integration.py tests/test_subagent_msg_role.py tests/test_call_subagent_memory_hook.py -v 2>&1 | tail -40
```
**预期**：全部通过。

- [ ] **Step 4.4**：如果任何测试失败，立即撤销改动恢复原状（铁律 #5 调试无效马上撤销）
```bash
cd <repo_root>
# 不用 git checkout（铁律 #8），用 Edit 工具精确回退两个改动点
```

---

### Task 5: 真实端到端验证（真实定时任务 + 真实 LLM）

**目标**：用真实定时任务触发主 Agent 上下文 84% 强制压缩，验证 context-manager 一次性输出 `keep=/update=/cursor=`、runner.py 成功解析、压缩完成、不再有 24 条 subagent_msg 污染主 Agent 请求。

**铁律 #5 要求**：测试必须用真实数据 + 真实 LLM，不 mock。

- [ ] **Step 5.1**：清理测试环境
```bash
cd <repo_root>
# 杀掉所有 niu 进程（铁律 #7 必须优雅退出，不能 pkill -f niu）
ps aux | grep -E "niu|launcher" | grep -v grep
# 用 kill -TERM <pid> 逐个优雅退出
```

- [ ] **Step 5.2**：检查 messages.db 状态 + 必要时恢复压缩前副本

**背景**：当前 messages.db 处于压缩失败后的异常状态（含 24 条 `role: subagent_msg` 污染消息）。如果带着污染的 db 跑端到端验证，可能因 db 状态本身失败，误判修复无效。用户提供了压缩前的副本 `~/.niu/messages.db_副本`（12:00 的版本，压缩前正常状态）。

```bash
# 1. 检查 messages.db 的 role 分布
sqlite3 ~/.niu/messages.db "SELECT role, COUNT(*) FROM messages GROUP BY role"
```

**预期输出（db 被污染的情况）**：
```
assistant|N
subagent_msg|24    ← 这是污染的标志
system|1
user|N
```

**预期输出（db 干净的情况）**：
```
assistant|N
system|1
user|N
```
（不含 `subagent_msg` 行）

**判断逻辑**：
- 如果输出含 `subagent_msg` 行 → db 被污染，走恢复分支
- 如果输出不含 `subagent_msg` 行 → db 干净，跳过恢复，直接进入 Step 5.3

**恢复分支（仅 db 被污染时执行）**：
```bash
# 2. 备份当前异常 db（用于事后排查污染成因）
cp ~/.niu/messages.db ~/.niu/messages.db.bak.corrupt

# 3. 从压缩前副本恢复（12:00 的版本，压缩前正常状态）
cp ~/.niu/messages.db_副本 ~/.niu/messages.db

# 4. 验证恢复后的 db 不含 subagent_msg
sqlite3 ~/.niu/messages.db "SELECT role, COUNT(*) FROM messages GROUP BY role"
```

**恢复后预期输出**：不含 `subagent_msg` 行（只有 assistant/system/user，或只有 system+user 的空 db）。

**铁律约束**：
- 备份异常 db 用 `cp` 拷贝，禁止 `mv` 或 `rm` 原文件（铁律 #2 未经同意不得覆盖仓库内任何备份 — 这里的备份是 db 副本，不是 git 备份，但同精神：先拷贝一份异常状态，再覆盖）
- 恢复前必须确认 `~/.niu/messages.db_副本` 存在且是压缩前的版本（用 `ls -la ~/.niu/messages.db_副本` 检查时间戳，应为 12:00 左右）
- 如果 `~/.niu/messages.db_副本` 不存在或时间戳不对，**停下来报告用户**，不要盲目恢复

- [ ] **Step 5.3**：清理上轮压缩残留（避免污染本轮测试）
```bash
# 检查 ~/.niu/ 下的 messages db 和 compress_plan
ls -la ~/.niu/
ls -la ~/.niu/compress_plan.json 2>/dev/null  # 应不存在或为空
```

- [ ] **Step 5.4**：启动程序
```bash
cd <repo_root>
./niu &
# 等待启动完成，看到 "LightRAG initialized" 和 API ready 日志
```

- [ ] **Step 5.5**：触发定时任务让主 Agent 上下文涨到 84%
- 方式一：跑一段长任务（如让主 Agent 处理大量文件、做长对话）直到日志出现 `Proactive compress: ... (84% > 80%)`
- 方式二：临时调小 `~/.niu/preferences.json` 的 `contextWindowSize` 或 `warningThreshold`，让小量对话就触发压缩

```bash
# 监控日志
tail -f logs/api_stderr.log | grep -E "Proactive compress|context-manager|keep=|FORMAT_ERROR|No keep= line"
```

- [ ] **Step 5.6**：验证压缩一次性成功
**预期日志序列**（按顺序）：
1. `[Context] Proactive compress: NNNNN/NNNNNN tokens (84% > 80%)` — 触发压缩回调
2. `[Tidy] Force: context-manager completed, length=NNN` — context-manager 一次返回
3. `[Runner] Force: Parsed from content: keep=K, delete=D, update=U, cursor_idx=C` — 解析成功
4. 没有 `[AtPrefix]` 拦截日志
5. 没有 `[对话格式错误]` 提示
6. 没有 `No keep= line found in sub-agent reply` 异常
7. 主 Agent 下一轮请求里没有 24 条 subagent_msg（用 db 查询验证：`sqlite3 ~/.niu/messages.db "SELECT role, COUNT(*) FROM messages GROUP BY role"`）

- [ ] **Step 5.7**：测试完彻底杀进程（铁律 #7）
```bash
# 用 kill -TERM 优雅退出，禁止 pkill -f niu
ps aux | grep -E "niu|launcher" | grep -v grep | awk '{print $2}' | xargs -I {} kill -TERM {}
# 等待 5 秒让进程优雅退出
sleep 5
ps aux | grep -E "niu|launcher" | grep -v grep  # 应为空
```

---

### Task 6: 提交修复

- [ ] **Step 6.1**：检查改动范围
```bash
cd <repo_root>
git status
git diff agent/subagent.py agent/generic/agent_loop.py
```
**预期**：只有两个文件改动 + 一个新测试文件 + 这个计划文件。

- [ ] **Step 6.2**：提交修复
```bash
cd <repo_root>
git add agent/subagent.py agent/generic/agent_loop.py tests/test_context_manager_bypass_at_prefix.py docs/superpowers/plans/2026-07-08-context-manager-bypass-at-prefix.md
git commit -m "$(cat <<'EOF'
fix(context-manager): 绕过 @niu-agent/@end 守则注入和拦截层

同步异步子 Agent 调用改造（阶段一/二/三/四）给所有子 Agent 强制
注入 @niu-agent/@end 守则并强制拦截无 @ 前缀的输出，误伤了
context-manager 的原生 keep=/update=/cursor= 输出格式。

错误一：守则注入污染 context-manager 系统提示词，LLM 困惑该输出
keep= 还是 @end。
错误二：拦截层把 keep=/update=/cursor= 误判为 FORMAT_ERROR，append
[对话格式错误] 提示词让 LLM 重跑，第二轮 LLM 困惑后输出 @end 放弃，
runner.py 抛 ValueError("No keep= line found")，压缩完全失败。

修复（最小改动，只改两个点）：
1. agent/subagent.py L501-503：注入守则时加 agent_name != "context-manager"
   判断绕过。
2. agent/generic/agent_loop.py _intercept_at_prefix_content 入口：加
   _subagent_unique_name == "context-manager" 判断直接返回 NO_INTERCEPTION。

原有 runner.py 和 compat.py 的 keep=/update=/cursor= 解析逻辑完全不动。

判断方式用 agent_name 显式比对，最直接最不易误伤其他子 Agent
（file-processor/dream-evolver 等仍该有守则、该被拦截）。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 6.3**：git 操作后修复文件权限（铁律 #7）
```bash
cd <repo_root>
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null
```

- [ ] **Step 6.4**：验证提交成功
```bash
cd <repo_root>
git log --oneline -3
git status
```

---

## Self-Review

### 改动最小化检查

- [x] **只改两个代码点**：subagent.py 注入 + agent_loop.py 拦截
- [x] **不动原有 keep=/update=/cursor= 解析逻辑**：runner.py L1310-1337 / compat.py L2216-2234 / L2851-2876 完全不动
- [x] **不动 context-manager.md 系统提示词**：原本就没有 @niu/@end，本次也不改
- [x] **不动 call_subagent / call_subagent_with_auto_answer 签名**：判断在拦截层和注入层内部做
- [x] **新增测试文件**：不破坏现有测试

### 判断 context-manager 的方式可靠性

- [x] **注入点用 `agent_name == "context-manager"`**：函数参数直接可用，最直接
- [x] **拦截点用 `_subagent_unique_name == "context-manager"`**：拦截层签名没有 agent_name 参数，但 handler 上有 `_subagent_unique_name`，同步路径下 == agent_name
- [x] **不会误伤其他子 Agent**：file-processor / dream-evolver / entity-extractor / journal-agent / event-manager / browser-operator 都不在绕过名单里
- [x] **不会误伤主 Agent**：主 Agent 的 `_subagent_unique_name` 是空字符串或 None，不匹配 "context-manager"

### 保留原有逻辑检查

- [x] **runner.py L1310-1337 不动**：解析 keep=/update=/cursor= 写 messages 数据库的逻辑完全保留
- [x] **compat.py L2216-2234 / L2851-2876 不动**：模式二/三的解析逻辑完全保留
- [x] **context-manager.md 不动**：系统提示词不变，context-manager 仍按原设计输出 keep=/update=/cursor=

### 测试覆盖检查

- [x] **不注入**：`test_context_manager_system_prompt_has_no_at_niu_guide`
- [x] **不拦截**：`test_context_manager_keep_output_not_intercepted` + `test_context_manager_bypass_doesnt_append_format_error`
- [x] **不追加 FORMAT_ERROR 提示**：`test_context_manager_bypass_doesnt_append_format_error` 验证 messages 长度不变
- [x] **其他子 Agent 仍受约束**：`test_file_processor_system_prompt_still_has_at_niu_guide` + `test_file_processor_still_intercepted_when_no_at_prefix`
- [x] **回归测试**：Task 4 跑现有 at_prefix / general_subagent / call_subagent_with_auto_answer 等测试套件
- [x] **真实端到端**：Task 5 用真实定时任务 + 真实 LLM 触发 84% 上下文压缩

### 引入新 bug 的风险

- [x] **风险一：context-manager 改完仍输出 @niu-agent 询问**（极少见情况，比如它需要问主 Agent"压缩目标"）
  - 评估：context-manager.md 的系统提示词原本就没有 @niu-agent 守则，它的设计是"直接输出 keep=/update=/cursor="，不需要问主 Agent。模式三的 prompt（compat.py `_build_force_prompt`）也明确说"禁止调用任何工具，直接在回复中输出"
  - 结论：低风险，不需要额外处理
- [x] **风险二：call_subagent_with_auto_answer 的 while 循环**（subagent.py L965-975）会因为 context-manager 不再返回 `[unique_name] question` 格式而直接退出
  - 评估：`_extract_unique_name(result, agent_name)` 返回 None 时 `call_subagent_with_auto_answer` 直接 return，正是我们要的行为
  - 结论：无风险
- [x] **风险三：如果将来 context-manager 走异步路径**（`unique_name == "context-manager-xxxx"`，绕过判断不匹配）
  - 评估：当前所有 context-manager 调用都是同步路径（compat.py L1862/L1936/L2007/L2175/L2398/L2563/L2636/L2707 + runner.py L1257 都调 `call_subagent_with_auto_answer`，同步）
  - 结论：当前无风险；如果将来改造为异步，需要同步更新绕过判断（如改为 `unique_name.startswith("context-manager")`）— 在计划注释里标注
- [x] **风险四：context-manager 的输出碰巧以 @niu-agent 或 @end 开头**（极少见）
  - 评估：context-manager.md 的 prompt 要求输出 `<analysis>...</analysis>\nkeep=...\nupdate=...\ncursor=...`，不会以 @ 开头
  - 结论：低风险；即便发生，拦截层绕过后程序会拿到原始 content 去 parse keep=，找不到 keep= 时会抛 ValueError，但这种情况理论上不会发生

---

## Execution Handoff

执行顺序（**严格按 Task 0 → 6 顺序**）：

1. **Task 0**：临时备份提交（铁律 #3）
2. **Task 1**：写失败测试（TDD），跑测试确认 3 个失败、2 个通过
3. **Task 2**：改 subagent.py 注入点，跑 2 个注入侧测试确认通过
4. **Task 3**：改 agent_loop.py 拦截点，跑全部 5 个测试确认通过
5. **Task 4**：跑现有测试套件回归验证（不破坏其他子 Agent）
6. **Task 5**：真实端到端验证（真实定时任务 + 真实 LLM，铁律 #5）
7. **Task 6**：提交修复 + 修复文件权限（铁律 #7）

**关键约束**：
- 每个 Step 都要打勾 `- [ ]` → `- [x]`
- 任何 Step 失败立即停下，不要继续
- 调试无效立即撤销改动恢复原状（铁律 #5）
- 派出去的子 Agent 必须遵守所有铁律（特别是 #3 备份、#5 真实测试、#7 修权限、#8 不 pkill）
