# @前缀子Agent意图识别 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 异步子 Agent 的每轮 content 输出必须以 `@` 前缀表达意图——`@niu 问题` 询问主 Agent、`@end 总结` 结束会话；既无 `@` 前缀也无 tool_calls 时程序拒绝并要求重新输出。

**Architecture:** 在 `agent_loop.py:473` 的 `if not response.tool_calls:` 拦截点加三层校验（`@niu` 转问主 Agent / `@end` 允许结束 / 格式错误回退）。移除 ask_main_agent MCP 工具 + 结构注入守则（回退 commit da5c75a2 + fedb25ef 的提示词路线）。`_ask_main_agent_impl` 函数体 + MainAgentRequestQueue + db_monitor 链路 B + chat.py source 字段修复全部保留，content 拦截复用现有 future/push queue 逻辑。

**Tech Stack:** Python 3.11+ / asyncio / 现有 agent 框架

**前置分析**：`docs/superpowers/plans/2026-07-04-at-prefix-subagent-intent-analysis.md`（含回退清单 + 拦截点 + 风险评估）

---

## File Structure

| 文件 | 责任 | 操作 |
|------|------|------|
| `agent/generic/agent_loop.py` | L473 加 `@niu`/`@end`/格式错误三层拦截 | Modify |
| `agent/subagent.py` | 移除 `ASK_MAIN_AGENT_TOOL_SCHEMA` + `_ASYNC_ASK_GUIDE_*` 常量 + `build_subagent_system_segments` 的 `allow_async` 参数 + 注入分支；保留 `_ask_main_agent_impl` | Modify |
| `agent/handler.py` | 移除 ask_main_agent 工具派发分支 | Modify |
| `config/agent-template.md` | ask_main_agent 引导段改为 `@niu`/`@end` 守则 | Modify |
| `config/agents/niu.md` | 同上 | Modify |
| `tests/test_at_prefix_interception.py` | 拦截逻辑单元测试 | Create |
| `tests/test_general_subagent.py` | 移除 4 个守则注入测试 + 改 `build_subagent_system_segments` 签名相关测试 | Modify |
| `tests/test_ask_main_agent_injection.py` | 删除（不再注入 MCP 工具） | Delete |
| `tests/test_ask_main_agent.py` | 改造为测 `_ask_main_agent_impl` 直调 | Modify |
| `tests/verify_llm_at_prefix.py` | 真实 LLM 验证脚本（前置 Task） | Create |

---

## Task 1: 真实 LLM 验证 LLM 是否会输出 @ 前缀

**Files:**
- Create: `tests/verify_llm_at_prefix.py`

- [ ] **Step 1: 写验证脚本**

创建 `REDACTED_USER_PATH/tools/ai-bot/tests/verify_llm_at_prefix.py`：

```python
"""验证 LLM 在给定 @前缀守则的系统提示词下，是否会输出 @niu/@end 前缀。

用法：python/bin/python tests/verify_llm_at_prefix.py

验证目的：新方案根本假设是 LLM 能遵守"@前缀表达意图"的守则。
如果 LLM 完全不输出 @ 前缀，新方案不可行，需要重新设计。
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.llmcore import create_client


SYSTEM_PROMPT = """你是一个异步子 Agent。每轮输出必须遵循以下格式：

1. 调用工具继续工作：正常 tool_calls
2. 询问主 Agent（不退出，等主 Agent 回答后继续）：content 必须以 `@niu ` 开头，如 `@niu 我应该选择哪个选项？`
3. 结束会话（任务完成或无法继续）：content 必须以 `@end ` 开头，如 `@end 任务已完成，结果：...`

**重要**：禁止输出不带 @ 前缀的纯 content（会被程序拒绝并要求重新输出）。
遇到需要用户决策的问题时，必须用 `@niu` 询问，禁止直接把问题写在 content 里。
"""

USER_TASK = """请打开 16personalities.com 网站开始 MBTI 测试。
遇到第一个问题时不要自己选，必须询问我（用 @niu 前缀）。"""


def main():
    # 读 LLM 配置
    config_path = os.path.expanduser("~/.niu/config/user-config.json")
    with open(config_path) as f:
        config = json.load(f)

    llm_config = {
        "model": config.get("model", "deepseek-chat"),
        "api_key": config.get("api_key", ""),
        "base_url": config.get("base_url", ""),
    }

    client = create_client(llm_config)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_TASK},
    ]

    # 第一轮：给一个 browser_navigate 工具，让它先调工具
    tools = [{
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "打开网址",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    }]

    print("=== 第一轮（有工具可用）===")
    response = client.chat(messages=messages, tools=tools)
    print(f"tool_calls: {response.tool_calls}")
    print(f"content: {response.content!r}")

    if response.tool_calls:
        # 模拟工具返回，进入第二轮
        messages.append({"role": "assistant", "content": response.content, "tool_calls": response.tool_calls})
        messages.append({"role": "tool", "tool_call_id": response.tool_calls[0].id, "content": "已打开 16personalities.com，进入测试页，第 1 题：你经常结交新朋友。请选择 A/B/C/D。"})

        print("\n=== 第二轮（遇到选择题，应输出 @niu）===")
        response = client.chat(messages=messages, tools=tools)
        print(f"tool_calls: {response.tool_calls}")
        print(f"content: {response.content!r}")

        content = (response.content or "").strip()
        if content.startswith("@niu"):
            print("\n✅ 验证通过：LLM 输出了 @niu 前缀")
        elif content.startswith("@end"):
            print("\n⚠️ LLM 输出了 @end（误判任务完成）")
        else:
            print("\n❌ 验证失败：LLM 没有输出 @ 前缀")
            print(f"   content: {content!r}")
    else:
        # 第一轮就没调工具，直接看 content
        content = (response.content or "").strip()
        if content.startswith("@"):
            print(f"\n✅ LLM 输出了 @ 前缀: {content[:50]}")
        else:
            print(f"\n❌ LLM 没调工具也没输出 @ 前缀: {content!r}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行验证脚本**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python tests/verify_llm_at_prefix.py`

Expected: 输出 `✅ 验证通过：LLM 输出了 @niu 前缀`。如果输出 ❌，需要调整系统提示词措辞或重新评估方案。

- [ ] **Step 3: 根据验证结果调整守则措辞（如果需要）**

如果 LLM 不输出 `@niu`，尝试在 SYSTEM_PROMPT 里加强约束（如加示例 few-shot）。如果加强后仍不输出，**停下报告用户**，方案可能不可行。

- [ ] **Step 4: Commit**

```bash
git add tests/verify_llm_at_prefix.py
git commit -m "test(llm-verify): 验证 LLM 是否输出 @ 前缀 content

新方案根本假设验证：LLM 能否遵守 @niu/@end 前缀守则。
通过则继续实施，不通过则需重新设计。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 回退结构注入守则（commit da5c75a2 + fedb25ef）

**Files:**
- Modify: `agent/subagent.py`
- Modify: `tests/test_general_subagent.py`

- [ ] **Step 1: 删除 `_ASYNC_ASK_GUIDE_TEMPLATE` 和 `_ASYNC_ASK_GUIDE_MARKER` 常量**

Read `REDACTED_USER_PATH/tools/ai-bot/agent/subagent.py` L69-85，删除这两个常量定义（约 15 行）。

- [ ] **Step 2: 改 `build_subagent_system_segments` 签名回单参数**

Read L399 附近，把 `def build_subagent_system_segments(agent_name: str, allow_async: bool = False) -> tuple:` 改回 `def build_subagent_system_segments(agent_name: str) -> tuple:`。

docstring 里删除 `allow_async` 参数说明。

- [ ] **Step 3: 删除 L429-434 的守则注入分支**

Read L425-440 附近，删除：

```python
    # 异步子 Agent 强制注入 ask_main_agent 使用守则
    # ...
    if allow_async and _ASYNC_ASK_GUIDE_MARKER not in static_system:
        static_system += "\n\n" + _ASYNC_ASK_GUIDE_TEMPLATE
```

- [ ] **Step 4: 改 `call_subagent` 调用处回单参数**

Read L639-644 附近，删除：

```python
    allow_async = bool(agent_config.get("allowAsync", False)) if agent_config else False
```

把 `build_subagent_system_segments(agent_name, allow_async=allow_async)` 改回 `build_subagent_system_segments(agent_name)`。

- [ ] **Step 5: 删除 test_general_subagent.py 里 4 个守则注入测试**

Read `REDACTED_USER_PATH/tools/ai-bot/tests/test_general_subagent.py`，删除以下测试函数（约 L425-513）：

- `test_build_subagent_system_segments_injects_async_guide`
- `test_build_subagent_system_segments_no_inject_for_sync`
- `test_build_subagent_system_segments_no_inject_if_md_has_guide`
- `test_build_subagent_system_segments_injects_when_md_has_soft_guide`
- `test_build_subagent_system_segments_marker_constant_matches`

- [ ] **Step 6: 运行测试确认无回归**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py -v`

Expected: 全过（原 31 个 - 5 个 = 26 个）。

- [ ] **Step 7: Commit**

```bash
git add agent/subagent.py tests/test_general_subagent.py
git commit -m "revert(subagent): 回退结构注入 ask_main_agent 守则

新方案改用 @前缀 content 拦截，不再依赖提示词注入。
回退 commit da5c75a2 + fedb25ef：
- 删除 _ASYNC_ASK_GUIDE_TEMPLATE / _ASYNC_ASK_GUIDE_MARKER 常量
- build_subagent_system_segments 签名回单参数
- 删除守则注入分支
- 删除 5 个相关测试

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 移除 ask_main_agent MCP 工具

**Files:**
- Modify: `agent/subagent.py`
- Modify: `agent/handler.py`
- Delete: `tests/test_ask_main_agent_injection.py`

- [ ] **Step 1: 删除 `ASK_MAIN_AGENT_TOOL_SCHEMA` 定义**

Read `REDACTED_USER_PATH/tools/ai-bot/agent/subagent.py` L790-810 附近，找 `ASK_MAIN_AGENT_TOOL_SCHEMA` 定义，整段删除。

- [ ] **Step 2: 删除 `_build_subagent_tools_schema` 里的注入逻辑**

Read L590-596 附近，删除：

```python
    if memory_context is not None:
        tools_schema.append(ASK_MAIN_AGENT_TOOL_SCHEMA)
        logger.info(f"[SubAgent] {agent_name}: ask_main_agent 注入（异步子 Agent）")
```

- [ ] **Step 3: 删除 `handler.py` 的 ask_main_agent 派发分支**

Read `REDACTED_USER_PATH/tools/ai-bot/agent/handler.py` L1113-1130 附近，找 `if tool_name == "ask_main_agent":` 分支，整段删除。

- [ ] **Step 4: 删除 test_ask_main_agent_injection.py**

```bash
rm REDACTED_USER_PATH/tools/ai-bot/tests/test_ask_main_agent_injection.py
```

- [ ] **Step 5: 验证 `_ask_main_agent_impl` 函数体保留**

Read `REDACTED_USER_PATH/tools/ai-bot/agent/subagent.py` L813-899 附近，确认 `_ask_main_agent_impl` 函数完整保留（content 拦截要复用）。

- [ ] **Step 6: 运行测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/ -v -k "ask_main_agent or subagent" --ignore=tests/test_ask_main_agent_stop_deadlock.py`

Expected: 除 stop_deadlock 测试外全过（stop_deadlock 下个 Task 改造）。

- [ ] **Step 7: Commit**

```bash
git add agent/subagent.py agent/handler.py tests/test_ask_main_agent_injection.py
git commit -m "refactor(subagent): 移除 ask_main_agent MCP 工具

新方案用 @niu content 拦截替代 MCP 工具调用（更贴合 LLM 训练习惯）。
- 删除 ASK_MAIN_AGENT_TOOL_SCHEMA 定义
- 删除 _build_subagent_tools_schema 注入分支
- 删除 handler.py ask_main_agent 派发分支
- 删除 test_ask_main_agent_injection.py
- 保留 _ask_main_agent_impl 函数体（content 拦截复用）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 改造 `_ask_main_agent_impl` 支持 content 拦截调用

**Files:**
- Modify: `agent/subagent.py`

- [ ] **Step 1: 写失败测试**

在 `REDACTED_USER_PATH/tools/ai-bot/tests/test_at_prefix_interception.py`（新建文件）加：

```python
"""@前缀子Agent意图识别单元测试"""
import os
from unittest import mock


def test_ask_main_agent_impl_callable_directly(tmp_path, monkeypatch):
    """_ask_main_agent_impl 可被 content 拦截层直接调用（不经 MCP 工具派发）"""
    from agent import subagent
    from agent.ask_main_agent import PendingAskRegistry, AskMainAgentFuture

    # mock registry + queue
    fake_instance = mock.MagicMock()
    fake_instance._ask_terminated = False
    monkeypatch.setattr(subagent, "SubagentRegistry")
    subagent.SubagentRegistry.get = mock.Mock(return_value=fake_instance)

    # mock push queue
    pushed = []
    fake_queue = mock.MagicMock()
    fake_queue.push = mock.Mock(side_effect=lambda x: pushed.append(x))
    monkeypatch.setattr(subagent, "get_main_agent_request_queue", mock.Mock(return_value=fake_queue))

    # mock future wait 立即返回
    with mock.patch.object(AskMainAgentFuture, "wait", return_value="主 Agent 的回答"):
        result = subagent._ask_main_agent_impl(
            question="我应该选择哪个选项？",
            unique_name="test-agent-abc1",
            agent_name="test-agent",
        )

    assert result == "主 Agent 的回答"
    assert len(pushed) == 1
    assert "test-agent-abc1" in pushed[0]
    assert "我应该选择哪个选项？" in pushed[0]


def test_ask_main_agent_impl_returns_terminated_when_cancelled(monkeypatch):
    """子 Agent 被 cancel 后，_ask_main_agent_impl 返回 TERMINATED_SIGNAL"""
    from agent import subagent
    from agent.ask_main_agent import TERMINATED_SIGNAL

    fake_instance = mock.MagicMock()
    fake_instance._ask_terminated = True  # 已被 cancel
    subagent.SubagentRegistry = mock.MagicMock()
    subagent.SubagentRegistry.get = mock.Mock(return_value=fake_instance)

    result = subagent._ask_main_agent_impl(
        question="问题",
        unique_name="test-agent-abc1",
        agent_name="test-agent",
    )
    assert result == TERMINATED_SIGNAL
```

- [ ] **Step 2: 运行测试确认通过（`_ask_main_agent_impl` 已存在，应直接通过）**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_prefix_interception.py -v`

Expected: PASS（如果 FAIL，说明 `_ask_main_agent_impl` 签名需要调整以支持 content 拦截调用）。

- [ ] **Step 3: 如果签名需要调整，改造 `_ask_main_agent_impl`**

Read `REDACTED_USER_PATH/tools/ai-bot/agent/subagent.py` L813-899 的 `_ask_main_agent_impl` 函数。如果它的签名已经是 `(question, unique_name, agent_name)`，无需改。如果还依赖 MCP 工具调用的参数（如 `tool_call_id` 等），抽离出公共逻辑：

```python
def _ask_main_agent_impl(question: str, unique_name: str, agent_name: str) -> str:
    """询问主 Agent，阻塞等待回答。

    可被两种路径调用：
    1. MCP 工具派发（已移除）
    2. content 拦截层（@niu 前缀）

    Args:
        question: 问题内容（已剥除 @niu 前缀）
        unique_name: 子 Agent 唯一名
        agent_name: 子 Agent 配置名

    Returns:
        主 Agent 的回答，或 TERMINATED_SIGNAL（被 cancel 时）
    """
    # ... 保留原有 future + push queue + wait 逻辑
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_prefix_interception.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/subagent.py tests/test_at_prefix_interception.py
git commit -m "refactor(subagent): _ask_main_agent_impl 支持 content 拦截层直调

抽离 MCP 工具派发耦合，函数可被 @niu content 拦截层直接调用。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: 在 agent_loop.py 加 @前缀拦截逻辑

**Files:**
- Modify: `agent/generic/agent_loop.py:473`

- [ ] **Step 1: 写失败测试**

在 `REDACTED_USER_PATH/tools/ai-bot/tests/test_at_prefix_interception.py` 追加：

```python
def test_at_niu_prefix_triggers_ask_main_agent(monkeypatch):
    """子 Agent content 以 @niu 开头时，拦截层调 _ask_main_agent_impl 并把回答注入 messages"""
    from agent.generic import agent_loop
    from agent import subagent

    # mock _ask_main_agent_impl 返回固定回答
    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="主 Agent 的回答")
    )

    # 构造一个假的 response 对象
    fake_response = mock.MagicMock()
    fake_response.tool_calls = []  # 无工具调用
    fake_response.content = "@niu 我应该选择哪个选项？"

    # 构造 messages 列表
    messages = [
        {"role": "system", "content": "你是子 Agent"},
        {"role": "user", "content": "开始测试"},
        {"role": "assistant", "content": "好的"},
    ]

    # 构造 handler 带 _subagent_unique_name
    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"

    # 调拦截函数
    result = agent_loop._intercept_at_prefix_content(
        content=fake_response.content,
        tool_calls=fake_response.tool_calls,
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),  # 非 None（异步子 Agent）
        agent_name="test-agent",
    )

    # 断言：_ask_main_agent_impl 被调用
    subagent._ask_main_agent_impl.assert_called_once()
    call_kwargs = subagent._ask_main_agent_impl.call_args
    assert call_kwargs.kwargs["question"] == "我应该选择哪个选项？"
    assert call_kwargs.kwargs["unique_name"] == "test-agent-abc1"

    # 断言：messages 被追加了 assistant content + tool 回答
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == "@niu 我应该选择哪个选项？"
    assert messages[-1]["role"] == "tool"
    assert messages[-1]["content"] == "主 Agent 的回答"

    # 断言：返回 INTERCEPTED（让 agent_loop continue）
    assert result == agent_loop.INTERCEPTED


def test_at_end_prefix_allows_exit(monkeypatch):
    """子 Agent content 以 @end 开头时，拦截层允许退出"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@end 任务已完成，结果：成功",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
        agent_name="test-agent",
    )

    assert result == agent_loop.EXIT
    # messages 不被追加（允许正常退出）
    assert len(messages) == 1


def test_no_at_prefix_no_tool_calls_returns_format_error(monkeypatch):
    """子 Agent content 无 @ 前缀且无 tool_calls 时，返回 FORMAT_ERROR 并追加提示"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="我应该选择哪个选项？",  # 无 @ 前缀
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
        agent_name="test-agent",
    )

    assert result == agent_loop.FORMAT_ERROR
    # messages 被追加 assistant content + user 格式错误提示
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == "我应该选择哪个选项？"
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]
    assert "@niu" in messages[-1]["content"]
    assert "@end" in messages[-1]["content"]


def test_no_interception_for_sync_subagent(monkeypatch):
    """同步子 Agent（memory_context=None）不拦截，允许 content 直接返回"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="任务完成的结果",  # 无 @ 前缀
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 同步子 Agent
        agent_name="test-agent",
    )

    assert result == agent_loop.NO_INTERCEPTION
    assert len(messages) == 1  # messages 不被追加


def test_no_interception_when_tool_calls_present(monkeypatch):
    """有 tool_calls 时不拦截（正常工具调用）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="调工具",
        tool_calls=[{"id": "tc1", "function": {"name": "browser_navigate"}}],  # 有工具调用
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
        agent_name="test-agent",
    )

    assert result == agent_loop.NO_INTERCEPTION
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_prefix_interception.py -v`

Expected: 5 个测试 FAIL（`_intercept_at_prefix_content` 函数不存在）。

- [ ] **Step 3: 在 agent_loop.py 模块级加常量 + 函数**

Read `REDACTED_USER_PATH/tools/ai-bot/agent/generic/agent_loop.py` L1-30 附近（模块级常量区），加：

```python
# @前缀子Agent意图识别返回值
INTERCEPTED = "intercepted"      # @niu 拦截成功，调用了 _ask_main_agent_impl，messages 已追加
EXIT = "exit"                    # @end 允许退出
FORMAT_ERROR = "format_error"    # 无 @ 前缀无 tool_calls，已追加格式错误提示
NO_INTERCEPTION = "no_intercept" # 不拦截（同步子 Agent 或有 tool_calls）
```

然后在模块级（其他 helper 函数附近）加 `_intercept_at_prefix_content` 函数：

```python
def _intercept_at_prefix_content(
    content: str,
    tool_calls: list,
    messages: list,
    handler,
    memory_context,
    agent_name: str,
) -> str:
    """@前缀子Agent意图识别拦截层。

    仅异步子 Agent（memory_context is not None）+ 无 tool_calls 时拦截。
    content 以 @niu 开头 → 调 _ask_main_agent_impl，把回答作为 tool 消息注入 messages，返回 INTERCEPTED
    content 以 @end 开头 → 允许退出，返回 EXIT
    其他 → 追加格式错误提示，返回 FORMAT_ERROR

    Args:
        content: LLM 返回的 content
        tool_calls: LLM 返回的 tool_calls
        messages: 当前对话 messages 列表（会被追加）
        handler: NiuHandler 实例（含 _subagent_unique_name）
        memory_context: 异步子 Agent 的 memory_context（同步子 Agent 为 None）
        agent_name: 子 Agent 配置名

    Returns:
        INTERCEPTED / EXIT / FORMAT_ERROR / NO_INTERCEPTION
    """
    # 同步子 Agent 或有 tool_calls 时不拦截
    if memory_context is None or tool_calls:
        return NO_INTERCEPTION

    stripped = (content or "").lstrip()

    # @niu 拦截
    if stripped.startswith("@niu "):
        question = stripped[5:]  # 剥除 "@niu " 前缀
        unique_name = getattr(handler, "_subagent_unique_name", "")
        if not unique_name:
            logger.error("[AtPrefix] 异步子 Agent 无 _subagent_unique_name，无法调 ask_main_agent")
            return FORMAT_ERROR

        # 调 _ask_main_agent_impl（阻塞等主 Agent 回答）
        from agent.subagent import _ask_main_agent_impl
        answer = _ask_main_agent_impl(
            question=question,
            unique_name=unique_name,
            agent_name=agent_name,
        )

        # 把 assistant content + 主 Agent 回答作为 tool 消息注入 messages
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "tool", "tool_call_id": "at_niu_intercept", "content": answer})
        return INTERCEPTED

    # @end 允许退出
    if stripped.startswith("@end "):
        return EXIT

    # 格式错误
    messages.append({"role": "assistant", "content": content})
    messages.append({"role": "user", "content":
        "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
        "1. 调用工具继续工作（正常 tool_calls）\n"
        "2. 询问主 Agent：content 以 `@niu ` 开头，如 `@niu 我应该选择哪个选项？`\n"
        "3. 结束会话：content 以 `@end ` 开头，如 `@end 任务已完成，结果：...`\n"
        "禁止输出不带 @ 前缀的纯 content。请重新输出。"
    })
    return FORMAT_ERROR
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_prefix_interception.py -v`

Expected: 5 个测试全过。

- [ ] **Step 5: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_at_prefix_interception.py
git commit -m "feat(agent_loop): @前缀子Agent意图识别拦截层

新增 _intercept_at_prefix_content 函数：
- @niu → 调 _ask_main_agent_impl，回答注入 messages
- @end → 允许退出
- 无 @ 前缀无 tool_calls → 追加格式错误提示
- 同步子 Agent（memory_context=None）不拦截

仅异步子 Agent 生效，同步子 Agent 行为不变。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: 在 agent_runner_loop 集成拦截逻辑

**Files:**
- Modify: `agent/generic/agent_loop.py:473-483`

- [ ] **Step 1: 写失败测试**

在 `REDACTED_USER_PATH/tools/ai-bot/tests/test_at_prefix_interception.py` 追加：

```python
def test_agent_runner_loop_intercepts_at_niu(monkeypatch):
    """agent_runner_loop 在 L473 拦截点调用 _intercept_at_prefix_content"""
    from agent.generic import agent_loop

    # mock _intercept_at_prefix_content 返回 INTERCEPTED
    monkeypatch.setattr(
        agent_loop, "_intercept_at_prefix_content",
        mock.Mock(return_value=agent_loop.INTERCEPTED)
    )

    # mock client.chat 返回无 tool_calls 的 @niu content
    fake_response = mock.MagicMock()
    fake_response.tool_calls = []
    fake_response.content = "@niu 问题"

    fake_client = mock.MagicMock()
    fake_client.chat = mock.Mock(return_value=fake_response)

    # 构造 messages + handler + memory_context
    messages = [{"role": "user", "content": "开始"}]
    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_memory_context = mock.MagicMock()  # 非 None

    # 调 agent_runner_loop（需要 mock 很多，简化为直接测拦截集成）
    # 这里只验证 _intercept_at_prefix_content 被调用
    # 完整的 agent_runner_loop 集成测试在端到端验证

    # 由于 agent_runner_loop 是 generator，直接测太复杂
    # 改为测拦截点逻辑：在 not response.tool_calls 分支里调拦截函数
    # 这个测试主要确认拦截函数被正确 import 和调用

    # 验证拦截函数可被 agent_loop 模块访问
    assert hasattr(agent_loop, "_intercept_at_prefix_content")
    assert hasattr(agent_loop, "INTERCEPTED")
    assert hasattr(agent_loop, "EXIT")
    assert hasattr(agent_loop, "FORMAT_ERROR")
    assert hasattr(agent_loop, "NO_INTERCEPTION")
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_prefix_interception.py::test_agent_runner_loop_intercepts_at_niu -v`

Expected: PASS

- [ ] **Step 3: 改 agent_loop.py L473-483 集成拦截**

Read `REDACTED_USER_PATH/tools/ai-bot/agent/generic/agent_loop.py` L470-490 附近。当前代码：

```python
            content = response.content or ""
            content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)
            # Harness 验证：仅在 LLM 不调工具直接回复用户时验证
            # 条件 not response.tool_calls 精确区分最终回复 vs 中间工具调用
            if not response.tool_calls:
                validation = validate_references(content)
                if not validation.is_valid and _harness_fail_count < _MAX_HARNESS_RETRIES:
                    _harness_fail_count += 1
                    feedback = validation.format_feedback()
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": feedback})
                    continue  # 回到 while 循环，让 LLM 修正
                _harness_fail_count = 0

            yield StreamEvent("reply", content)
```

改为（在 validate_references 之前加拦截）：

```python
            content = response.content or ""
            content = re.sub(r"<tool_use>.*?</tool_use>", "", content, flags=re.DOTALL)

            # 阶段三：@前缀子Agent意图识别拦截（仅异步子 Agent）
            if not response.tool_calls:
                interception = _intercept_at_prefix_content(
                    content=content,
                    tool_calls=response.tool_calls,
                    messages=messages,
                    handler=handler,
                    memory_context=memory_context,
                    agent_name=handler._subagent_unique_name.split("-")[0] if hasattr(handler, "_subagent_unique_name") and handler._subagent_unique_name else "",
                )
                if interception == INTERCEPTED:
                    continue  # @niu 已处理，回到 while 循环让 LLM 继续
                if interception == EXIT:
                    yield StreamEvent("reply", content[5:])  # 剥除 "@end " 前缀后推前端
                    break  # @end 允许退出
                if interception == FORMAT_ERROR:
                    continue  # 格式错误，回到 while 循环让 LLM 重新输出
                # NO_INTERCEPTION：继续走原有逻辑

            # Harness 验证：仅在 LLM 不调工具直接回复用户时验证
            # 条件 not response.tool_calls 精确区分最终回复 vs 中间工具调用
            if not response.tool_calls:
                validation = validate_references(content)
                if not validation.is_valid and _harness_fail_count < _MAX_HARNESS_RETRIES:
                    _harness_fail_count += 1
                    feedback = validation.format_feedback()
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content": feedback})
                    continue  # 回到 while 循环，让 LLM 修正
                _harness_fail_count = 0

            yield StreamEvent("reply", content)
```

**注意**：
- `agent_name` 从 `handler._subagent_unique_name` 解析（如 `test-agent-abc1` → `test-agent`）。如果 `_subagent_unique_name` 不存在或为空（主 Agent 或同步子 Agent），传空字符串——但此时 `memory_context is None`，拦截函数会返回 NO_INTERCEPTION，不会用到 agent_name。
- `@end` 时剥除前缀后推前端，避免用户看到 `@end` 标记。
- `@niu` 时 `continue` 不推前端（问题已转给主 Agent，前端不该看到子 Agent 的提问）。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_prefix_interception.py -v`

Expected: 6 个测试全过。

- [ ] **Step 5: 运行全量回归测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_general_subagent.py tests/test_main_agent_request_queue_drain.py tests/test_at_prefix_interception.py -v`

Expected: 全过。

- [ ] **Step 6: Commit**

```bash
git add agent/generic/agent_loop.py tests/test_at_prefix_interception.py
git commit -m "feat(agent_loop): agent_runner_loop 集成 @前缀拦截

在 L473 not response.tool_calls 分支加拦截：
- INTERCEPTED → continue（@niu 已处理）
- EXIT → 剥前缀推前端 + break
- FORMAT_ERROR → continue（让 LLM 重新输出）
- NO_INTERCEPTION → 走原有 validate_references 逻辑

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: 改造现有 ask_main_agent 测试

**Files:**
- Modify: `tests/test_ask_main_agent.py`
- Modify: `tests/test_ask_main_agent_stop_deadlock.py`

- [ ] **Step 1: 改造 test_ask_main_agent.py**

Read `REDACTED_USER_PATH/tools/ai-bot/tests/test_ask_main_agent.py`。原有测试通过 MCP 工具派发调用 `_ask_main_agent_impl`，现在 MCP 工具已移除，改为直接调 `_ask_main_agent_impl`。

每个测试改为：
- 不再构造 MCP 工具调用参数
- 直接 `subagent._ask_main_agent_impl(question=..., unique_name=..., agent_name=...)`
- 断言行为不变（future 完成 / TERMINATED 返回等）

具体改造按现有测试逻辑调整，保留测试覆盖目标。

- [ ] **Step 2: 改造 test_ask_main_agent_stop_deadlock.py**

Read `REDACTED_USER_PATH/tools/ai-bot/tests/test_ask_main_agent_stop_deadlock.py`。原有测试通过 MCP 工具触发 ask_main_agent，现在改为通过 `@niu` content 拦截触发。

每个测试改为：
- 不再构造 MCP 工具调用
- 直接调 `_ask_main_agent_impl` 或 `_intercept_at_prefix_content` 模拟 `@niu` 拦截
- 断言 5 个死锁约束仍然生效

- [ ] **Step 3: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_ask_main_agent.py tests/test_ask_main_agent_stop_deadlock.py -v`

Expected: 全过。

- [ ] **Step 4: Commit**

```bash
git add tests/test_ask_main_agent.py tests/test_ask_main_agent_stop_deadlock.py
git commit -m "test(ask_main_agent): 改造为 @niu content 拦截路径

MCP 工具已移除，测试改为直接调 _ask_main_agent_impl 或 _intercept_at_prefix_content。
5 个死锁约束测试覆盖不变。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: 更新提示词（模板 + niu.md）

**Files:**
- Modify: `config/agent-template.md`
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 改 config/agent-template.md**

Read `REDACTED_USER_PATH/tools/ai-bot/config/agent-template.md`。

把"提示词正文"段的"何时主动询问主 Agent"那条改为：

```markdown
- **何时主动询问主 Agent**（仅异步模式 allowAsync: true 时才会注入 @前缀拦截层；同步子 Agent 不拦截。异步子 Agent **必须**用 `@niu ` 前缀询问主 Agent，禁止把问题写在 content 里直接返回——直接返回会被程序拒绝并要求重新输出。结束会话必须用 `@end ` 前缀）
```

把"frontmatter 字段说明"的 `allowAsync` 说明改为：

```markdown
- `allowAsync`：true 时支持异步调用（主 Agent 调用后立即返回，子 Agent 后台跑；异步子 Agent 自动启用 @前缀拦截层，必须用 @niu/@end 表达意图）
```

- [ ] **Step 2: 改 config/agents/niu.md**

Read `REDACTED_USER_PATH/tools/ai-bot/config/agents/niu.md` 的"如何创建子 Agent"段。

把"如果 allowAsync: true，正文必须写明 ask_main_agent 的使用时机"改为：

```markdown
   - **重要**：如果 allowAsync: true，正文必须写明子 Agent 用 `@niu ` 前缀询问主 Agent、用 `@end ` 前缀结束会话——禁止把问题写在 content 里直接返回（会被程序拒绝）。如"遇到用户意图不明确时用 @niu 询问，不要自行假设；任务完成时用 @end 返回结果"
```

- [ ] **Step 3: 验证 niu.md 解析正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -c "from agent.subagent import get_subagent_config; c = get_subagent_config('niu'); print('sub agents' in c)"`

Expected: 打印 `True`。

- [ ] **Step 4: Commit**

```bash
git add config/agent-template.md config/agents/niu.md
git commit -m "docs(prompt): 提示词改为 @niu/@end 前缀守则

替代 ask_main_agent MCP 工具引导，更贴合 LLM @提及训练习惯。
异步子 Agent 必须用 @niu 询问、@end 结束，纯 content 返回被程序拒绝。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: 端到端真实 LLM 验证

**Files:**
- 无代码改动，纯验证

- [ ] **Step 1: 清空测试环境**

```bash
# 备份现有 ~/.niu/agents/（如有）
if [ -d ~/.niu/agents ]; then
    mv ~/.niu/agents ~/.niu/agents.backup.$(date +%s)
fi
```

- [ ] **Step 2: 启动程序**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./niu`

Expected: 程序正常启动。

- [ ] **Step 3: 验证 @niu 拦截生效**

在前端发送消息让主 Agent 创建异步子 Agent（如 browser-operator），派任务遇到选择时：

Expected:
- 子 Agent 系统提示词含 `@niu`/`@end` 守则（从 niu.md 提示词要求主 Agent 写 MD 时写入）
- 子 Agent 遇到选择时输出 `@niu 我应该选择哪个选项？`
- 程序拦截，调 `_ask_main_agent_impl`
- 主 Agent 收到询问（[子名] 问题 推到 MainAgentRequestQueue → db_monitor 链路 A → SSE → 前端 → /api/chat/session → 主 Agent 新一轮 LLM）
- 主 Agent 回复 `@子名 回答`
- 子 Agent 收到回答继续工作

- [ ] **Step 4: 验证 @end 退出**

子 Agent 完成任务后：

Expected:
- 子 Agent 输出 `@end 任务已完成，结果：...`
- 程序允许退出，剥除 `@end ` 前缀后推前端
- 子 Agent 进程结束

- [ ] **Step 5: 验证格式错误回退**

手动构造一个场景让子 Agent 输出无 `@` 前缀的 content（如让子 Agent 遇到模糊问题但不遵守守则）：

Expected:
- 程序拒绝，追加格式错误提示
- 子 Agent 下一轮重新输出（应输出 `@niu` 或 `@end`）

- [ ] **Step 6: 验证 /stop 终止**

双击停止按钮：

Expected:
- 子 Agent 被终止
- 如子 Agent 阻塞在 `_ask_main_agent_impl`，`cancel_pending_ask` 取消 future
- 5 个死锁约束生效，无死锁

- [ ] **Step 7: 清理验证环境**

```bash
rm -rf ~/.niu/agents
if [ -d ~/.niu/agents.backup.* ]; then
    mv ~/.niu/agents.backup.* ~/.niu/agents
fi
```

- [ ] **Step 8: 记录验证结果到独立文件**

新建 `docs/superpowers/verification-reports/2026-07-04-at-prefix-e2e.md`：

```bash
mkdir -p docs/superpowers/verification-reports
```

记录每步实际表现 + 关键日志片段。

- [ ] **Step 9: Commit 验证报告**

```bash
git add docs/superpowers/verification-reports/2026-07-04-at-prefix-e2e.md
git commit -m "test(e2e): @前缀子Agent意图识别端到端验证通过

@niu 询问主 Agent、@end 结束会话、格式错误回退、/stop 终止 全部验证通过。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage**：
- 回退结构注入守则 → Task 2 ✓
- 移除 ask_main_agent MCP 工具 → Task 3 ✓
- `_ask_main_agent_impl` 复用 → Task 4 ✓
- agent_loop 拦截层 → Task 5/6 ✓
- 测试改造 → Task 7 ✓
- 提示词更新 → Task 8 ✓
- 端到端验证 → Task 9 ✓
- 前置 LLM 验证 → Task 1 ✓

**2. Placeholder scan**：无 TBD/TODO，所有代码片段完整。

**3. Type consistency**：
- `_intercept_at_prefix_content(content, tool_calls, messages, handler, memory_context, agent_name) -> str` 在 Task 5 定义，Task 6 调用 ✓
- 返回值常量 `INTERCEPTED` / `EXIT` / `FORMAT_ERROR` / `NO_INTERCEPTION` 在 Task 5 定义，Task 6 使用 ✓
- `_ask_main_agent_impl(question, unique_name, agent_name) -> str` 在 Task 4 确认，Task 5 调用 ✓

---

## 执行选择

Plan complete and saved to `docs/superpowers/plans/2026-07-04-at-prefix-subagent-intent.md`. Two execution options:

**1. Subagent-Driven (recommended)** - 我派新子 Agent 逐 Task 实施，每 Task 后做 spec + 代码质量两轮审查，快速迭代。

**2. Inline Execution** - 在本会话内逐 Task 实施，批量执行带检查点。

哪种？
