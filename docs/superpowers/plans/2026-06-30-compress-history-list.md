# context-manager 模式二消息传递改造计划 v2（审查后修订）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 context-manager 模式二的"319 条消息序列化成单条 763KB user message"改造为"直接传 messages 列表（history），每条 content 加简易 idx 前缀"，避免单条 message 超火山方舟单消息 token 上限。

**Architecture:** 新增 `_build_compress_history` 构造 history 列表（每条 message 原样保留 role/tool_calls/tool_call_id，content 开头加 `[idx:N] Ntokens ` 前缀），**同步排除孤立 tool 消息**（父 assistant 被 PROTECTED 排除时其 tool 消息也排除，保证 LLM 看到的 idx 连续）。`run_context_manager_mode2` 模式二分支调 `_build_compress_history` 替代 `_build_incremental_msg_text`，通过 `call_subagent` 的 `history` 参数传入（已存在，无需新增）。模式一保持原逻辑不变。`call_subagent` 的 `history` 参数透传链已完整（call_subagent → _run_agent_loop → agent_runner_loop）。

**Tech Stack:** Python 3.11, litellm, 火山方舟 OpenAI 兼容协议

---

## 问题分析

### 当前结构（单消息超限根因）

`niu_api/compat.py:1782-1791` 的 `run_context_manager_mode2`：
1. L1669 `_build_incremental_msg_text`（模式一/二共用）把消息序列化成 `[id:UUID] [idx:N] Ntokens role: content` 纯文本
2. 拼进 `prompt` f-string（L1742-1764），含压缩指令 + 全量序列化文本
3. 作为 `task` 传给 `call_subagent`（L1785）
4. `call_subagent` 把 `task` 作为 `initial_user_content` → `agent_runner_loop` append 为**单条 user message**（agent_loop.py:271-273）

319 条消息合并成 763KB 单条 user message，触发火山方舟单消息 token 上限。

### 关键认知

1. **总量不超限**：115178 tokens < 200K 上下文窗口。问题在**单条 message 体积**。
2. **context-manager 是 fork 主 Agent 上下文**：应直接看 messages 列表。
3. **简易 idx 编号**：用户要求用简易编号，不用 UUID。程序内存维护 idx→真实 ID 映射。
4. **`call_subagent` 的 `history` 参数已存在**（subagent.py:388）：透传链完整（call_subagent → _run_agent_loop → agent_runner_loop），无需新增任何参数。
5. **`agent_runner_loop` history 处理**（agent_loop.py:220-268）：支持 user/assistant/tool 三种 role + tool_calls 还原 + **孤立 tool 消息过滤**（L257-261，tool_call_id 不在 _valid_tc_ids 则跳过）。

### v1 审查发现的关键 bug（v2 修复）

1. **孤立 tool 消息导致 idx 错位**（阻断）：PROTECTED 排除 assistant(tool_calls) 后，其 tool 消息若不在保护集（tool 不算 protect_recent 计数），会进 history 但被 agent_runner_loop 过滤 → LLM 看到的 idx 不连续 → 与 `_idx_to_id` 错位 → 删除/更新错误消息。**v2 修复**：`_build_compress_history` 同步排除孤立 tool（父 assistant 被排除则其 tool 也排除）。
2. **模式一/二共用 L1669**（阻断）：计划"删除 L1669 调用"会破坏模式一。**v2 修复**：L1669 改为分支判断——模式二调 `_build_compress_history`，模式一保持 `_build_incremental_msg_text`。
3. **测试调用方式错误**（阻断）：`_tidy_context_impl` 签名是 `(request: dict, ...)`，且 `get_message_store` 不是 `_get_message_store_async`。**v2 修复**：重写测试用 dict 调用 + 正确 mock。
4. **变量名臆造**（重要）：计划用 `compress_messages`/`msg_tokens_list`，实际是 `messages`/`msg_tokens`。**v2 修复**：用实际变量名。
5. **context-manager.md L267 未列入改造**（重要）。**v2 修复**：明确包含 L267 输入规范段落。

### 设计原则

1. **直接传 messages 列表**：每条 message 原样，content 加 idx 前缀。
2. **简易 idx 前缀**：`[idx:N] Ntokens `（短）。
3. **同步排除孤立 tool**：父 assistant 被排除则其 tool 也排除，保证 LLM 看到的 idx 连续。
4. **模式一不动**：L1669 改为分支，模式二走新函数，模式一走原函数。
5. **不改 `agent_runner_loop` / `call_subagent` / `_run_agent_loop` 签名**：history 参数已存在。
6. **idx→真实 ID 映射沿用**：`compress_msg_ids` + `_idx_to_id`（L1799-1801）不变，由新函数的 `out_msg_ids` 收集。
7. **压缩结果解析不变**：`keep=`/`update=` 解析（L1803-1822）不变。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `niu_api/compat.py` | 新增 `_build_compress_history`；L1669 改为分支；`run_context_manager_mode2` 用 history | Modify |
| `config/agents/context-manager.md` | L23 + L267 输入格式说明改为 messages 列表 + idx 前缀 | Modify |
| `tests/test_compress_history.py` | 验证 `_build_compress_history` + 集成测试 | Create |

---

## Task 1: 实现 `_build_compress_history`（含孤立 tool 同步排除）

**Files:**
- Modify: `niu_api/compat.py`（新增 `_build_compress_history` 函数）
- Test: `tests/test_compress_history.py`

- [ ] **Step 1: 写失败测试 — `_build_compress_history` 基本构造 + 孤立 tool 排除**

Create `tests/test_compress_history.py`:

```python
"""context-manager 模式二 history 构造测试。"""
import sys
from pathlib import Path

# 确保 niu_api 可 import
_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _build_compress_history


class FakeMsg:
    """模拟 Message 对象（compat.py 用 getattr(msg, 'id') 等访问）。"""
    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


def test_build_compress_history_basic():
    """基本场景：3 条消息（user/assistant/user）构造 history，每条 content 加 idx 前缀。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
        FakeMsg(id="msg-3", role="user", content="今天天气"),
    ]
    msg_tokens = [10, 20, 15]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
    )

    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"].startswith("[idx:1] 10tokens ")
    assert "你好" in history[0]["content"]
    assert history[1]["role"] == "assistant"
    assert history[1]["content"].startswith("[idx:2] 20tokens ")
    assert history[2]["role"] == "user"
    assert history[2]["content"].startswith("[idx:3] 15tokens ")
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_compress_history_with_tool_calls():
    """assistant 带 tool_calls + tool 消息：保留 tool_calls/tool_call_id，content 加前缀。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="查天气"),
        FakeMsg(
            id="msg-2", role="assistant", content="",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="今天晴", tool_call_id="tc-1"),
    ]
    msg_tokens = [5, 8, 12]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
    )

    assert len(history) == 3
    assert history[1]["role"] == "assistant"
    assert history[1]["tool_calls"] == messages[1].tool_calls
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "tc-1"
    assert history[2]["content"].startswith("[idx:3] 12tokens ")


def test_build_compress_history_protected_excludes_orphan_tool():
    """PROTECTED 排除 assistant(tool_calls) 后，其 tool 消息也同步排除（避免孤立 tool 导致 idx 错位）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="远端消息"),
        FakeMsg(
            id="msg-2", role="assistant", content="远端回复",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="tool 输出", tool_call_id="tc-1"),
        FakeMsg(id="msg-4", role="user", content="近端消息"),  # 受保护
    ]
    msg_tokens = [10, 20, 30, 15]

    # protect_recent=1：最后 1 条 user/assistant 受保护 → msg-4 受保护
    # exclude_protected=True：msg-4 排除
    # 关键：msg-2(assistant, tool_calls) 不在保护集（protect_recent 只数最后1条 user/assistant = msg-4）
    # 所以 msg-2 不被排除，msg-3(tool) 也不被排除（父 assistant 在）
    # 此场景下 history 应含 msg-1, msg-2, msg-3（msg-4 排除）
    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=1,
        exclude_protected=True,
    )

    # msg-4 被排除，其余 3 条保留，idx 连续 1,2,3
    assert len(history) == 3
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_compress_history_protected_assistant_excludes_its_tool():
    """PROTECTED 排除 assistant(tool_calls) 时，其 tool 消息也同步排除（孤立 tool 检测）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="远端"),
        FakeMsg(
            id="msg-2", role="assistant", content="远端回复",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "tool_x", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="tool 输出", tool_call_id="tc-1"),
        FakeMsg(id="msg-4", role="assistant", content="近端回复"),  # 受保护
    ]
    msg_tokens = [10, 20, 30, 15]

    # protect_recent=1：最后 1 条 user/assistant = msg-4 受保护
    # exclude_protected=True：msg-4 排除
    # msg-2(assistant, tool_calls) 不在保护集，保留
    # msg-3(tool, tc-1) 父 assistant msg-2 在，保留
    # 此场景 history 应含 msg-1, msg-2, msg-3
    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=1,
        exclude_protected=True,
    )

    assert len(history) == 3
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}

    # 现在构造另一个场景：protect_recent=2，msg-2 和 msg-4 都受保护
    # msg-2 被排除 → msg-3(tool, tc-1) 父 assistant 不在 → 孤立 tool，必须同步排除
    history2, idx_to_id2 = _build_compress_history(
        messages=messages,
        msg_tokens=msg_tokens,
        out_msg_ids=None,
        protect_recent=2,
        exclude_protected=True,
    )
    # msg-2 和 msg-4 被排除，msg-3 孤立 tool 同步排除，只剩 msg-1
    assert len(history2) == 1
    assert idx_to_id2 == {1: "msg-1"}


def test_build_compress_history_out_msg_ids():
    """out_msg_ids 收集保留消息的真实 ID（与 idx 顺序一致，含孤立 tool 同步排除）。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="a"),
        FakeMsg(id="msg-2", role="assistant", content="b"),
    ]
    out_msg_ids = []

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=[10, 20],
        out_msg_ids=out_msg_ids,
    )

    assert out_msg_ids == ["msg-1", "msg-2"]
    assert idx_to_id == {1: "msg-1", 2: "msg-2"}


def test_build_compress_history_no_tokens():
    """msg_tokens 为 None 时不加 tokens 前缀，只加 idx。"""
    messages = [FakeMsg(id="msg-1", role="user", content="你好")]

    history, idx_to_id = _build_compress_history(
        messages=messages,
        msg_tokens=None,
        out_msg_ids=None,
    )

    # 前缀格式 [idx:1] 内容（无 tokens）
    assert history[0]["content"].startswith("[idx:1] ")
    assert "你好" in history[0]["content"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_history.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_compress_history'`

- [ ] **Step 3: 实现 `_build_compress_history` 函数（含孤立 tool 同步排除）**

Read `niu_api/compat.py:303-386`（`_build_incremental_msg_text`）作为参考。

在 `_build_incremental_msg_text` 函数**之后**（约 L387）新增函数：

```python
def _build_compress_history(
    messages,
    msg_tokens: list | None = None,
    out_msg_ids: list | None = None,
    protect_recent: int = 0,
    exclude_protected: bool = False,
) -> tuple[list[dict], dict[int, str]]:
    """构造 context-manager 模式二的 history 列表（每条 message 加 idx 前缀）。

    与 _build_incremental_msg_text 的区别：
    - 输出 history 列表（role/content/tool_calls/tool_call_id 原样），而非序列化文本
    - content 开头加 `[idx:N] Ntokens ` 前缀（简易 idx，不用 UUID）
    - 单条 message 不会超限（每条就是原大小 + 前缀）
    - 同步排除孤立 tool 消息：若父 assistant 被 PROTECTED 排除，其 tool 消息也排除
      （避免 agent_runner_loop 过滤孤立 tool 导致 LLM 看到的 idx 不连续）

    Args:
        messages: 全量消息列表（Message 对象，含 id/role/content/tool_calls/tool_call_id）
        msg_tokens: 每条消息的 token 数列表（与 messages 等长），None 则不加 tokens 前缀
        out_msg_ids: 输出参数，收集保留消息的真实 ID 列表（与 idx 顺序一致）
        protect_recent: 对最后 N 条 user/assistant 消息加 PROTECTED 标签（0 表示不加）
        exclude_protected: True 则排除 PROTECTED 消息（不进 history、不进 out_msg_ids、不分配 idx）

    Returns:
        (history, idx_to_id):
        - history: [{"role":..., "content": "[idx:N] Ntokens ...原content", "tool_calls"?:..., "tool_call_id"?:...}, ...]
        - idx_to_id: {idx: 真实 message_id}，用于解析 context-manager 输出的 keep=/update=
    """
    if out_msg_ids is None:
        out_msg_ids = []

    total_count = len(messages)
    # 预计算保护位置：从尾部向前找 N 条 user/assistant 消息的相对位置
    _protected_positions: set[int] = set()
    if protect_recent > 0:
        _count = 0
        for rp in range(total_count - 1, -1, -1):
            m = messages[rp]
            if getattr(m, "role", "") in ("user", "assistant"):
                _protected_positions.add(rp)
                _count += 1
                if _count >= protect_recent:
                    break

    # 第一遍：确定哪些位置被排除（PROTECTED 排除 + 孤立 tool 同步排除）
    excluded_positions: set[int] = set()

    # 1) PROTECTED 排除
    if exclude_protected:
        for rp in _protected_positions:
            excluded_positions.add(rp)

    # 2) 孤立 tool 同步排除：若 tool 消息的父 assistant（持有对应 tool_call_id）被排除，则 tool 也排除
    #    收集所有被排除的 assistant 的 tool_call_id
    orphaned_tool_call_ids: set[str] = set()
    for rp in excluded_positions:
        m = messages[rp]
        if getattr(m, "role", "") == "assistant" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else ""
                if tc_id:
                    orphaned_tool_call_ids.add(tc_id)
    # 排除孤立 tool 消息
    for rp, m in enumerate(messages):
        if getattr(m, "role", "") == "tool":
            tc_id = getattr(m, "tool_call_id", "") or ""
            if tc_id in orphaned_tool_call_ids:
                excluded_positions.add(rp)

    # 第二遍：构造 history（只含未被排除的消息，idx 连续编号）
    history: list[dict] = []
    idx_to_id: dict[int, str] = {}
    display_idx = 0

    for rel_pos, msg in enumerate(messages):
        if rel_pos in excluded_positions:
            continue

        msg_id = getattr(msg, "id", "") or ""
        role = getattr(msg, "role", "user")
        content = getattr(msg, "content", "") or ""
        tool_calls = getattr(msg, "tool_calls", None)
        tool_call_id = getattr(msg, "tool_call_id", None)

        display_idx += 1
        out_msg_ids.append(msg_id)
        idx_to_id[display_idx] = msg_id

        # 构造前缀
        token_annotation = ""
        if msg_tokens and rel_pos < len(msg_tokens):
            token_annotation = f"{msg_tokens[rel_pos]}tokens "
        prefix = f"[idx:{display_idx}] {token_annotation}"

        # 构造 history entry（原样保留 role/tool_calls/tool_call_id）
        entry: dict = {"role": role, "content": prefix + content}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id

        history.append(entry)

    return history, idx_to_id
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_history.py -v`
Expected: 6 个测试全部 PASS

- [ ] **Step 5: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_compress_history.py
git commit -m "feat(compat): add _build_compress_history for message-list compression

构造 context-manager 模式二的 history 列表（每条 message 加 idx 前缀），
替代序列化成单条文本。同步排除孤立 tool 消息（父 assistant 被 PROTECTED
排除时其 tool 也排除），保证 LLM 看到的 idx 连续。"
```

---

## Task 2: 改造 L1669 为分支 + `run_context_manager_mode2` 用 history

**Files:**
- Modify: `niu_api/compat.py:1664-1791`（L1669 分支 + 模式二构造 history + 调 call_subagent）
- Test: `tests/test_compress_history.py`

- [ ] **Step 1: 写集成测试 — 模式二传 history 而非序列化文本**

在 `tests/test_compress_history.py` 追加：

```python
def test_mode2_passes_history_to_call_subagent(monkeypatch):
    """模式二应构造 history 列表传给 call_subagent，而非序列化文本塞进 task。"""
    import asyncio
    import niu_api.compat as compat

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    # mock runner 控制 usage_percent（>50 触发模式二）
    # 注意：compat.py 是函数内 import `from niu_api.chat import get_or_create_runner`
    # 必须 patch 源模块 niu_api.chat.get_or_create_runner，patch compat.get_or_create_runner 无效
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module
    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 120000})()  # 120K tokens
        llm_config = {}  # compat.py L1385 runner.llm_config 需要

    def fake_get_or_create_runner():
        return FakeRunner()

    # mock call_subagent 捕获参数，返回 keep= 方案，短路后续执行
    # 注意：compat.py L1375 是函数内 import `from agent.subagent import call_subagent`
    # 必须 patch 源模块 agent.subagent.call_subagent，patch compat.call_subagent 无效
    captured = {}
    def fake_call_subagent(agent_name, task, llm_config, mcp_client,
                           context_fifo_threshold, history=None, **kwargs):
        captured["agent_name"] = agent_name
        captured["task"] = task
        captured["history"] = history
        return "keep=1,2\nupdate="

    # mock 压缩执行（避免触发 chat_lock/DB 操作）
    async def fake_noop(*a, **kw):
        return None

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    # 关键：patch 源模块（compat.py 函数内 import 从 niu_api.chat 取）
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    # 关键：patch 源模块（compat.py 函数内 import 从 agent.subagent 取）
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    # _read_context_window_tokens 等配置读取 mock（这些是模块级 import，patch compat 正确）
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_target_threshold", lambda: 0.3, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)  # 不保护，2 条都进 history

    # 调用 _tidy_context_impl（request dict 形式）
    request = {"session_id": "test", "mode": "sleep"}
    try:
        asyncio.run(compat._tidy_context_impl(request))
    except Exception:
        pass  # 后续执行可能报错（未 mock 全部），只关心 call_subagent 是否被正确调用

    # 验证 call_subagent 收到 history 参数
    assert captured.get("agent_name") == "context-manager"
    assert captured.get("history") is not None
    assert isinstance(captured["history"], list)
    assert len(captured["history"]) == 2
    # task 是压缩指令（不含序列化消息文本）
    assert "CRITICAL" in captured["task"] or "压缩" in captured["task"]
    # task 不应含 [id:UUID] 格式（那是旧序列化文本的特征）
    assert "[id:" not in captured["task"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_history.py::test_mode2_passes_history_to_call_subagent -v`
Expected: FAIL（当前 `run_context_manager_mode2` 不传 history 参数）

- [ ] **Step 3: 改造 L1669 为分支 + 模式二构造 history**

Read `niu_api/compat.py:1664-1791` 完整段落确认上下文。

**改动 1**：L1664-1673 改为分支（模式二调 `_build_compress_history`，模式一保持 `_build_incremental_msg_text`）。

当前代码（L1664-1673）：
```python
            # 模式二：始终全量传入（无游标机制），模式一：增量范围
            _is_mode2 = usage_percent >= 50
            _compress_cursor = "" if _is_mode2 else last_compress_id
            _end_cursor = None if _is_mode2 else new_dream_id
            compress_msg_ids = []
            compress_msg_text = _build_incremental_msg_text(
                messages, _compress_cursor, compress_msg_ids, msg_tokens,
                end_cursor_id=_end_cursor, protect_recent=protect_recent_count,
                exclude_protected=True
            )
```

改为：
```python
            # 模式二：始终全量传入（无游标机制），模式一：增量范围
            _is_mode2 = usage_percent >= 50
            _compress_cursor = "" if _is_mode2 else last_compress_id
            _end_cursor = None if _is_mode2 else new_dream_id
            compress_msg_ids = []
            compress_history: list[dict] = []  # 模式二专用（替代 compress_msg_text）
            if _is_mode2:
                # 模式二：构造 history 列表（每条 message 加 idx 前缀），避免单条 user message 超限
                compress_history, _ = _build_compress_history(
                    messages, msg_tokens,
                    out_msg_ids=compress_msg_ids,
                    protect_recent=protect_recent_count,
                    exclude_protected=True,
                )
                compress_msg_text = ""  # 模式二不用序列化文本
            else:
                # 模式一：保持原序列化文本逻辑
                compress_msg_text = _build_incremental_msg_text(
                    messages, _compress_cursor, compress_msg_ids, msg_tokens,
                    end_cursor_id=_end_cursor, protect_recent=protect_recent_count,
                    exclude_protected=True
                )
```

**改动 2**：模式二的 prompt（L1742-1764）不再含 `compress_msg_text`，改为引用"上方历史消息"。

Read L1742-1764 确认当前 prompt。模式二分支的 prompt 改为：

```python
                    prompt = f"""CRITICAL: 你只有一轮机会完成压缩决策。禁止调用任何工具，直接回复压缩方案。

压缩上下文：当前{display_tokens} tokens（{usage_percent:.1f}%），需释放至{target_tokens} tokens以下。

上方历史消息每条开头带 [idx:N] Ntokens 前缀，共 {len(compress_history)} 条。
role=tool 的工具输出会被程序自动删除，不需要放入 keep。

直接回复两行文本，不要调用任何工具，不要输出其他任何内容：
第1行：keep=保留的idx序号，逗号分隔，支持范围如1-5
第2行：update=需压缩的idx序号|摘要内容，多个用分号分隔
示例：
keep=1,2,5-10,15
update=3|用户讨论了XX方案;11|工具执行了YY操作

压缩规则（必须遵守）：
- 按事务合并：属于同一件事的多轮交互（用户要求→工具调用→结果），合并为一条摘要
- 远端摘要格式："用户要求X，最终Y"（只保留意图和结果，丢弃过程）
- 近端摘要格式："用户要求X，调用Z工具，得到Y"（保留关键工具和输出）
- role=tool 的工具输出：不需要放入keep，会被程序自动删除
- 纯确认回复（"好的""明白了""谢谢"）：不需要放入keep
- 不在keep中的消息会被程序自动删除，所以有价值的对话必须放进keep或update
- update的idx必须在keep中
- 只输出这两行"""
```

**注意**：模式一分支的 prompt（L1766-1772，else 分支）保持原样，仍用 `compress_msg_text`。

**改动 3**：`run_context_manager_mode2`（L1782-1789）调 `call_subagent` 传 `history=compress_history`。

当前代码（L1782-1789）：
```python
                    def run_context_manager_mode2():
                        return call_subagent(
                            agent_name="context-manager",
                            task=prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
                        )
```

改为：
```python
                    def run_context_manager_mode2():
                        return call_subagent(
                            agent_name="context-manager",
                            task=prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
                            history=compress_history,  # 直接传 messages 列表，避免单条 user message 超限
                        )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_history.py -v`
Expected: 全部 PASS

- [ ] **Step 5: 验证现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/ -v 2>&1 | tail -20`
Expected: 无新增 FAIL（预存的 FAIL 与基线一致）

- [ ] **Step 6: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_compress_history.py
git commit -m "feat(compat): mode-2 passes history list instead of serialized text

L1669 改为分支：模式二调 _build_compress_history 构造 history 列表，
模式一保持 _build_incremental_msg_text。run_context_manager_mode2 通过
call_subagent 的 history 参数传入，替代把 319 条消息序列化成单条 763KB
user message。避免单条 message 超火山方舟单消息 token 上限。"
```

---

## Task 3: 调整 context-manager.md 提示词输入格式（L23 + L267）

**Files:**
- Modify: `config/agents/context-manager.md`

- [ ] **Step 1: Read 当前提示词全文**

Read `REDACTED_USER_PATH/tools/ai-bot/config/agents/context-manager.md`。

- [ ] **Step 2: grep 找所有引用旧格式的地方**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep -n '\[id:UUID\]\|\[id:\|消息列表（每条带' config/agents/context-manager.md`

确认所有需要改的位置（预期 L23 和 L267 附近）。

- [ ] **Step 3: 修改 L23 输入格式说明**

把原来的：
```
每条消息格式为 [id:UUID] [idx:N] Xtokens role: content
```

改为：
```
历史消息以 messages 列表形式提供（你看到的上方对话历史），每条消息的 content 开头带 [idx:N] Ntokens 前缀：
- idx:N 是简易序号（1, 2, 3...），用于你在 keep=/update= 输出中引用消息
- Ntokens 是该消息的 token 数，帮助你判断压缩哪些消息收益最大
- 程序在内存维护 idx→真实 ID 映射，你只需用 idx 输出方案

注意：UUID 不再出现在前缀中（太长浪费 token）。只用简易 idx 编号。
```

- [ ] **Step 4: 修改 L267 "输入规范"段落**

把原来的：
```
消息格式：[id:UUID] [idx:N] Xtokens role: content
```

改为：
```
输入格式：messages 列表（上方历史消息），每条 content 开头带 [idx:N] Ntokens 前缀。
```

- [ ] **Step 5: 修改 L264 "权威数据源"措辞**

grep `权威数据源` 确认位置（约 L264）。把原来的：
```
prompt中的消息列表是权威数据源
```
改为：
```
历史消息列表是权威数据源（每条带 [idx:N] 前缀）
```

- [ ] **Step 6: 确认 keep=/update= 输出格式说明不变**

grep `keep=` / `update=` 确认输出格式说明仍用 idx（语义不变）。

- [ ] **Step 7: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/agents/context-manager.md
git commit -m "docs(context-manager): update input format to messages list with idx prefix

L23 和 L267 输入格式说明改为 messages 列表 + idx 前缀，UUID 不再出现
（太长浪费 token），只用简易 idx 编号。keep=/update= 输出格式不变。"
```

---

## Task 4: 端到端验证（真实压缩触发）

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 启动程序，积累上下文到 50%+ 触发模式二**

用户执行：
1. `./niu` 启动程序
2. 持续对话，直到上下文使用率 ≥ 50%（日志显示 `usage=X.X%`）
3. 或等睡眠触发（`sleepTriggerMinutes` 到时触发模式二）

- [ ] **Step 2: 检查压缩请求日志结构**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "
import json, glob, os, datetime
files = sorted(glob.glob('logs/raw_http/' + datetime.date.today().strftime('%Y%m%d') + '/*_request.json'))
for f in reversed(files[-10:]):
    with open(f) as fh:
        req = json.load(fh)
    sys_content = req['messages'][0].get('content','')
    if isinstance(sys_content, str) and '记忆压缩器' in sys_content:
        print(f'=== 找到 context-manager 请求: {f} ===')
        msgs = req['messages']
        print(f'消息数: {len(msgs)}')
        for i, m in enumerate(msgs):
            c = m.get('content','')
            role = m.get('role')
            if isinstance(c, str):
                print(f'  [{i}] role={role} len={len(c)}')
            elif isinstance(c, list):
                print(f'  [{i}] role={role} list blocks={len(c)}')
        max_user_len = max((len(m.get('content','')) for m in msgs if m.get('role')=='user' and isinstance(m.get('content'),str)), default=0)
        print(f'最大 user message 长度: {max_user_len}')
        assert max_user_len < 100000, f'仍有单条 user message 超大: {max_user_len}'
        print('✅ 不再有单条超大 user message')
        break
"
```

Expected:
- 消息数 > 2（不再是 1 system + 1 巨大 user）
- 最大 user message 长度 < 100000（压缩指令很小）
- 有多条 user/assistant/tool 消息（history 列表展开）

- [ ] **Step 3: 验证压缩成功执行**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "Mode-2.*Plan parsed" logs/api_stderr.log 2>/dev/null | tail -3 || echo "检查 niu_api stderr 日志"`

Expected: `[Tidy] Mode-2: Plan parsed: N deletes, M updates (keep=K)`

- [ ] **Step 4: 验证无单消息超限错误**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "exceed max message tokens" logs/api_stderr.log 2>/dev/null | tail -3 || echo "无单消息超限错误"`

Expected: 不再出现 `Total tokens of image and text exceed max message tokens`。

- [ ] **Step 5: 最终提交（清理调试代码，如有）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git status
git add -A
git commit -m "feat(compress): context-manager mode-2 uses message list instead of serialized text

- _build_compress_history 构造 history 列表（每条 message 加 idx 前缀）
- 同步排除孤立 tool 消息（父 assistant 被排除则其 tool 也排除）
- L1669 分支：模式二走新函数，模式一保持原逻辑
- run_context_manager_mode2 通过 history 参数传入
- 避免单条 message 超火山方舟单消息 token 上限
- context-manager.md L23+L267 输入格式更新"
```

---

## 自审检查

### 1. Spec 覆盖

- `_build_compress_history` 构造 history（含孤立 tool 同步排除）→ Task 1 ✅
- L1669 分支（模式二/模式一分离）→ Task 2 Step 3 改动 1 ✅
- 模式二 prompt 不含序列化文本 → Task 2 Step 3 改动 2 ✅
- `run_context_manager_mode2` 传 history → Task 2 Step 3 改动 3 ✅
- `call_subagent` history 参数已存在 → 无需改（审查确认）✅
- context-manager.md L23 + L267 → Task 3 ✅
- idx→真实 ID 映射沿用 → Task 2（`compress_msg_ids` 由 `_build_compress_history` 的 out_msg_ids 收集）✅
- 压缩结果解析不变 → 不改 L1803-1822 ✅
- 端到端验证 → Task 4 ✅

### 2. Placeholder 扫描

无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性

- `_build_compress_history(messages, msg_tokens, out_msg_ids, protect_recent, exclude_protected) -> tuple[list[dict], dict[int, str]]`: 签名一致
- history entry: `{"role": str, "content": str, "tool_calls"?: list, "tool_call_id"?: str}`
- `call_subagent(..., history=list|None)`: 已存在
- idx_to_id: `dict[int, str]`

### 4. 边界条件

- messages 为空 → history 为空，idx_to_id 为空 ✅
- msg_tokens 为 None → 不加 tokens 前缀 ✅
- out_msg_ids 为 None → 内部初始化为空列表 ✅
- protect_recent=0 → 不保护任何消息 ✅
- exclude_protected=True + assistant 被排除 → 其 tool 消息同步排除 ✅
- tool 消息父 assistant 在 → 保留 ✅
- assistant 有 tool_calls 但无对应 tool 消息 → assistant 保留（tool_calls 会被 agent_runner_loop 过滤）✅

### 5. 向后兼容

- `_build_incremental_msg_text` 保留（模式一仍用）✅
- 模式一逻辑完全不变（L1669 else 分支保持原代码）✅
- `call_subagent` history 参数已存在，默认 None ✅
- `agent_runner_loop` / `_run_agent_loop` 签名不变 ✅
- idx→真实 ID 映射逻辑（`compress_msg_ids` + `_idx_to_id`）不变 ✅
- 压缩结果解析（`keep=`/`update=`）不变 ✅

### 6. 风险点

- **连续 user message**：history 末条是 user + task user message 连续。OpenAI/火山方舟通常允许，Task 4 实测确认。
- **火山方舟单消息上限**：每条 message（原大小 + 前缀）应不超限。若某条 tool 输出特别大（如照片结果），单条可能仍超限——预存问题，不在本计划范围。
- **`_idx_to_id` 与 LLM 看到的 idx 一致性**：`_build_compress_history` 同步排除孤立 tool 后，history 列表里的消息和 LLM 看到的消息一致（agent_runner_loop 不会再过滤，因为没有孤立 tool 了）。idx 连续。✅
- **测试 mock 完整性**：Task 2 Step 1 的集成测试 mock 了 `get_message_store`/`get_or_create_runner`/`call_subagent`/配置读取，但 `_tidy_context_impl` 后续执行（压缩执行）可能触发未 mock 的 chat_lock。测试用 try/except 短路，只验证 call_subagent 调用参数。

### 7. 不改动的部分

- `_build_incremental_msg_text` 函数（模式一用）
- `agent_runner_loop` 签名和 history 处理逻辑
- `_run_agent_loop` 签名
- `call_subagent` 签名（history 已存在）
- 模式一逻辑（L1669 else 分支）
- idx→真实 ID 映射（`compress_msg_ids` + `_idx_to_id`）
- 压缩结果解析（`keep=`/`update=`）
- 压缩执行逻辑（pause + chat_lock + DB 删除/更新）

### 8. v1 审查问题修复对照

| v1 审查问题 | v2 修复 |
|---------|---------|
| 孤立 tool 消息导致 idx 错位（阻断） | `_build_compress_history` 同步排除孤立 tool（父 assistant 被排除则其 tool 也排除）✅ |
| 模式一/二共用 L1669（阻断） | L1669 改为分支，模式二走新函数，模式一保持原逻辑 ✅ |
| 测试 `_tidy_context_impl` 调用方式错误（阻断） | 用 dict 调用 `{"session_id":"test","mode":"sleep"}` ✅ |
| 测试 `_get_message_store_async` 函数名错（阻断） | 改为 `get_message_store` ✅ |
| 测试断言 `"[idx:" not in task` 与 prompt 冲突（阻断） | 改为 `"[id:" not in task`（检测旧 UUID 格式，不含 `[id:`）✅ |
| 变量名臆造 `compress_messages`/`msg_tokens_list`（重要） | 用实际变量名 `messages`/`msg_tokens` ✅ |
| context-manager.md L267 未列入（重要） | Task 3 明确包含 L23 + L267 ✅ |
| `compress_msg_ids` 双重收集（重要） | L1669 分支后，模式二只调 `_build_compress_history` 收集，不再调 `_build_incremental_msg_text` ✅ |
| 测试 `fake_call_subagent` 返回值触发后续执行（重要） | 测试用 try/except 短路，只验证 call_subagent 参数 ✅ |
| `call_subagent` history 参数已存在（冗余） | v2 不再说"新增"，明确"已存在"✅ |
| v2 集成测试 mock `get_or_create_runner` 失效（函数内 import） | v2.1 改为 patch 源模块 `niu_api.chat.get_or_create_runner` ✅ |
| context-manager.md L264 "权威数据源"措辞偏移 | v2.1 Task 3 Step 5 同步调整措辞 ✅ |
| v2.1 FakeRunner 缺 `llm_config` 属性（L1385 AttributeError） | v2.2 FakeRunner 增加 `llm_config = {}` ✅ |
| v2.1 call_subagent mock 失效（L1375 函数内 import，patch compat 无效） | v2.2 改为 patch 源模块 `agent.subagent.call_subagent` ✅ |
| v2.2 protect_recent=10 + 仅 2 条 user/assistant → 全部排除 → context-manager 跳过 | v2.3 改为 `lambda: 0`（不保护，2 条都进 history，compress_msg_ids 非空触发 context-manager）✅ |
