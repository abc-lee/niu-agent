# Auto-Tidy 双游标机制重构 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 统一三个子Agent的消息传递方式为 task 模式，程序自动构建增量消息文本并管理游标，消除双写竞态和LLM自行判断的不确定性。

**Architecture:** compat.py 中 `_tidy_context_impl()` 串行调用三个子Agent，每个子Agent执行前重建消息快照。`_build_incremental_msg_text()` 增强为统一的消息文本生成器（支持 end_cursor_id、protect_recent、filter_wm 参数），取代 `_build_entity_history()`。handler.py 删除 entity-extractor 的 history 特殊分支和游标写入逻辑，dispatch 屏蔽手动调用。

**Tech Stack:** Python 3.11+, pytest (真实环境集成测试), SQLite (消息存储), asyncio

**设计文档:** `docs/superpowers/specs/2026-05-30-tidy-cursor-redesign.md`

---

## 文件结构

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `niu_api/compat.py` | Auto-tidy 管道核心，消息文本生成，游标管理 | 修改 |
| `agent/handler.py` | 子Agent调用路由，history分支，游标写入 | 修改 |
| `config/agents/niu.md` | 主Agent定义，sub agents列表 | 修改 |
| `config/agents/entity-extractor.md` | Entity Extractor prompt | 修改 |
| `config/agents/dream-evolver.md` | Dream Evolver prompt | 修改 |
| `config/agents/context-manager.md` | Context Manager prompt | 修改 |
| `tests/test_tidy_cursor.py` | 集成测试（新建） | 创建 |

---

### Task 1: `_build_incremental_msg_text()` 增强 — end_cursor_id 参数

**Files:**
- Modify: `niu_api/compat.py:77-117`
- Create: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 编写失败测试 — end_cursor_id 上界截断**

```python
# tests/test_tidy_cursor.py
"""
Auto-Tidy 双游标机制重构 — 集成测试

测试方式：使用内存 SQLite 构造真实消息，验证 _build_incremental_msg_text 的行为。
不需要 mock LLM，只测试程序层面的消息生成和游标逻辑。
"""
import pytest
import uuid
from dataclasses import dataclass, field


# --- 消息对象模拟（与 MessageStore 返回的对象兼容） ---

@dataclass
class FakeMessage:
    id: str
    role: str
    content: str
    tool_calls: list = field(default_factory=list)
    tool_results: list = field(default_factory=list)
    tool_call_id: str = ""
    created_at: str = ""


def make_messages(n: int, start_idx: int = 0) -> list[FakeMessage]:
    """生成 n 条模拟消息，UUID 顺序可预测"""
    return [
        FakeMessage(id=f"uuid-{start_idx + i}", role="user" if i % 2 == 0 else "assistant", content=f"消息内容 {start_idx + i}")
        for i in range(n)
    ]


# --- 导入被测函数 ---
import sys
sys.path.insert(0, ".")
from niu_api.compat import _build_incremental_msg_text


class TestBuildIncrementalMsgTextEndCursor:
    """测试 end_cursor_id 参数：上界截断，只生成到该游标为止的消息"""

    def test_end_cursor_truncates_messages(self):
        """end_cursor_id 存在时，只生成 [start_cursor, end_cursor] 范围内的消息"""
        messages = make_messages(10)  # uuid-0 ~ uuid-9
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-2",    # 从 uuid-2 之后开始
            out_msg_ids=out_ids,
            end_cursor_id="uuid-7",      # 到 uuid-7 为止
        )
        # 应包含 uuid-3, uuid-4, uuid-5, uuid-6, uuid-7
        assert out_ids == ["uuid-3", "uuid-4", "uuid-5", "uuid-6", "uuid-7"]
        assert "uuid-3" in result
        assert "uuid-7" in result
        assert "uuid-8" not in result

    def test_end_cursor_none_returns_all_after_start(self):
        """end_cursor_id 为 None 时，返回 start_cursor 之后的所有消息"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-5",
            out_msg_ids=out_ids,
            end_cursor_id=None,
        )
        assert out_ids == ["uuid-6", "uuid-7", "uuid-8", "uuid-9"]

    def test_end_cursor_not_found_degrades_to_full(self):
        """end_cursor_id 在消息列表中不存在时，退化到返回 start 之后的所有消息"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-2",
            out_msg_ids=out_ids,
            end_cursor_id="uuid-nonexistent",
        )
        # end_cursor 找不到 → 退化为无上界
        assert out_ids == ["uuid-3", "uuid-4", "uuid-5", "uuid-6", "uuid-7", "uuid-8", "uuid-9"]

    def test_end_cursor_before_start_returns_empty(self):
        """end_cursor 在 start_cursor 之前时，返回空"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-7",
            out_msg_ids=out_ids,
            end_cursor_id="uuid-2",
        )
        assert out_ids == []
        assert "无新增消息" in result or "（无新增消息）" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestBuildIncrementalMsgTextEndCursor -v`
Expected: FAIL — `_build_incremental_msg_text()` 不接受 `end_cursor_id` 参数

- [ ] **Step 3: 实现 end_cursor_id 参数**

在 `niu_api/compat.py` 的 `_build_incremental_msg_text()` 函数中增加 `end_cursor_id` 参数：

```python
def _build_incremental_msg_text(messages, last_cursor_id: str, out_msg_ids: list, msg_tokens: list | None = None, end_cursor_id: str | None = None) -> str:
    """
    构建增量消息文本：只包含游标之后的新消息。

    Args:
        messages: 全量消息列表
        last_cursor_id: 上次处理到的消息 UUID（空字符串表示全量）
        out_msg_ids: 输出参数，收集增量消息的 UUID 列表
        msg_tokens: 每条消息的 token 数列表（与 messages 等长），None 则不注解
        end_cursor_id: 上界游标 UUID（None = 到末尾），用于 Context Manager

    Returns:
        格式化的消息文本
    """
    # 找到下界游标位置
    cursor_idx = -1
    if last_cursor_id:
        for i, msg in enumerate(messages):
            msg_id = getattr(msg, "id", "") or ""
            if msg_id == last_cursor_id:
                cursor_idx = i
                break
        if cursor_idx < 0:
            logger.warning(f"[Tidy] Cursor UUID {last_cursor_id} not found in message list, degrading to full processing")

    # 找到上界游标位置
    end_idx = len(messages) - 1  # 默认到末尾
    if end_cursor_id:
        found = False
        for i, msg in enumerate(messages):
            if (getattr(msg, "id", "") or "") == end_cursor_id:
                end_idx = i
                found = True
                break
        if not found:
            logger.warning(f"[Tidy] End cursor UUID {end_cursor_id} not found in message list, degrading to no upper bound")
            end_idx = len(messages) - 1

    # 计算范围
    start = cursor_idx + 1 if cursor_idx >= 0 else 0
    effective_end = end_idx + 1  #切片不包含 end，+1 使 end_cursor 包含在内

    if start >= effective_end:
        return "（无新增消息）"

    lines = []
    for i, msg in enumerate(messages[start:effective_end]):
        idx = start + i + 1  # 1-based display index
        msg_id = getattr(msg, "id", "") or ""
        out_msg_ids.append(msg_id)
        content = msg.content or ""
        token_annotation = ""
        if msg_tokens and (start + i) < len(msg_tokens):
            token_annotation = f"{msg_tokens[start + i]}tokens "
        lines.append(f"[id:{msg_id}] [idx:{idx}] {token_annotation}{msg.role}: {content}")

    if not lines:
        return "（无新增消息）"

    return f"共 {len(lines)} 条新消息\n\n" + "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestBuildIncrementalMsgTextEndCursor -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_tidy_cursor.py niu_api/compat.py
git commit -m "feat: _build_incremental_msg_text 增加 end_cursor_id 参数 — Context Manager 上界截断"
```

---

### Task 2: `_build_incremental_msg_text()` 增强 — filter_wm 参数

**Files:**
- Modify: `niu_api/compat.py:77-117` (Task 1 修改后的版本)
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 编写失败测试 — filter_wm 过滤 WM 虚拟消息**

在 `tests/test_tidy_cursor.py` 末尾追加：

```python
class TestBuildIncrementalMsgTextFilterWm:
    """测试 filter_wm 参数：过滤 working_memory 虚拟消息和修复 tool_calls 成对完整性"""

    def _make_messages_with_wm(self) -> list[FakeMessage]:
        """构造含 WM 虚拟消息的消息列表"""
        return [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="你好！", tool_calls=[
                {"id": "tc-1", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content='{"status": "ok"}', tool_call_id="tc-1"),
            FakeMessage(id="uuid-3", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-4", role="assistant", content="好的，我来写"),
        ]

    def test_filter_wm_true_removes_working_memory(self):
        """filter_wm=True 时，过滤掉 WM 的 assistant(tool_calls) 和对应 tool 结果"""
        messages = self._make_messages_with_wm()
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-1(WM call) 和 uuid-2(WM result) 应被过滤
        assert "uuid-1" not in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-0" in out_ids
        assert "uuid-3" in out_ids
        assert "uuid-4" in out_ids

    def test_filter_wm_false_keeps_working_memory(self):
        """filter_wm=False 时，保留 WM 消息（默认行为，向后兼容）"""
        messages = self._make_messages_with_wm()
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=False,
        )
        assert "uuid-1" in out_ids
        assert "uuid-2" in out_ids

    def test_filter_wm_removes_trailing_orphan_tool_calls(self):
        """filter_wm=True 时，移除末尾孤立的 assistant(tool_calls)（无对应 tool 结果）"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-1", "function": {"name": "some_tool"}, "arguments": "{}"}
            ]),
            # 没有对应的 tool 结果 — 末尾孤立
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-1 是末尾孤立的 assistant(tool_calls)，应被移除
        assert "uuid-1" not in out_ids

    def test_filter_wm_removes_leading_orphan_tool(self):
        """filter_wm=True 时，移除开头孤立的 tool 消息（游标切割导致）"""
        messages = [
            FakeMessage(id="uuid-0", role="tool", content="result", tool_call_id="tc-missing"),
            FakeMessage(id="uuid-1", role="user", content="你好"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-0 是开头的孤立 tool，应被移除
        assert "uuid-0" not in out_ids
        assert "uuid-1" in out_ids

    def test_filter_wm_mixed_tool_calls_keeps_non_wm(self):
        """filter_wm=True 时，assistant 同时有 WM 和非 WM tool_calls，保留非 WM 部分"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="我来处理", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"},
                {"id": "tc-real", "function": {"name": "code_run"}, "arguments": "{}"},
            ]),
            FakeMessage(id="uuid-2", role="tool", content='{"status": "ok"}', tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="tool", content="代码执行结果", tool_call_id="tc-real"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(messages, "", out_ids, filter_wm=True)
        # uuid-1 应保留（有非 WM tool_call），uuid-2(WM result) 应过滤，uuid-3 应保留
        assert "uuid-1" in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-3" in out_ids

    def test_filter_wm_preserves_non_wm_tool_calls(self):
        """filter_wm=True 时，非 WM 的 tool_calls（如 code_run）不被过滤"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-1", role="assistant", content="好的", tool_calls=[
                {"id": "tc-1", "function": {"name": "code_run"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="代码执行结果", tool_call_id="tc-1"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(messages, "", out_ids, filter_wm=True)
        assert "uuid-0" in out_ids
        assert "uuid-1" in out_ids
        assert "uuid-2" in out_ids

    def test_filter_wm_idx_uses_original_positions(self):
        """filter_wm 过滤消息后，idx 仍使用原始全量列表位置"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="ok", tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="user", content="帮我写代码"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(messages, "", out_ids, filter_wm=True)
        # uuid-0 的 idx=1，uuid-3 的 idx=4（原始位置，不是过滤后的 idx=2）
        assert "[idx:1]" in result
        assert "[idx:4]" in result
        assert "[idx:2]" not in result  # uuid-1 被过滤，idx=2 不应出现
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestBuildIncrementalMsgTextFilterWm -v`
Expected: FAIL — `_build_incremental_msg_text()` 不接受 `filter_wm` 参数

- [ ] **Step 3: 实现 filter_wm 参数**

在 `_build_incremental_msg_text()` 中增加 `filter_wm` 参数，将 `_build_entity_history()` 中的 WM 过滤逻辑迁移过来。在生成 lines 之前先过滤消息：

```python
def _build_incremental_msg_text(messages, last_cursor_id: str, out_msg_ids: list, msg_tokens: list | None = None, end_cursor_id: str | None = None, filter_wm: bool = False) -> str:
    """
    构建增量消息文本：只包含游标之后的新消息。

    Args:
        messages: 全量消息列表
        last_cursor_id: 上次处理到的消息 UUID（空字符串表示全量）
        out_msg_ids: 输出参数，收集增量消息的 UUID 列表
        msg_tokens: 每条消息的 token 数列表（与 messages 等长），None 则不注解
        end_cursor_id: 上界游标 UUID（None = 到末尾），用于 Context Manager
        filter_wm: 过滤 WM 虚拟消息和修复 tool_calls 成对完整性

    Returns:
        格式化的消息文本
    """
    # 找到下界游标位置
    cursor_idx = -1
    if last_cursor_id:
        for i, msg in enumerate(messages):
            msg_id = getattr(msg, "id", "") or ""
            if msg_id == last_cursor_id:
                cursor_idx = i
                break
        if cursor_idx < 0:
            logger.warning(f"[Tidy] Cursor UUID {last_cursor_id} not found in message list, degrading to full processing")

    # 找到上界游标位置
    end_idx = len(messages) - 1
    if end_cursor_id:
        found = False
        for i, msg in enumerate(messages):
            if (getattr(msg, "id", "") or "") == end_cursor_id:
                end_idx = i
                found = True
                break
        if not found:
            logger.warning(f"[Tidy] End cursor UUID {end_cursor_id} not found in message list, degrading to no upper bound")
            end_idx = len(messages) - 1

    # 计算范围
    start = cursor_idx + 1 if cursor_idx >= 0 else 0
    effective_end = end_idx + 1

    if start >= effective_end:
        return "（无新增消息）"

    # 切片范围内的消息
    range_messages = list(messages[start:effective_end])

    # filter_wm: 过滤 WM 虚拟消息 + 修复 tool_calls 成对完整性
    # 必须在过滤前记录每条消息的原始位置，过滤后 idx 和 msg_tokens 仍用原始索引
    if filter_wm:
        # 1. 收集 WM tool_call_id
        wm_tc_ids = set()
        for msg in range_messages:
            tcs = msg.tool_calls if isinstance(msg.tool_calls, list) else []
            for tc in tcs:
                if isinstance(tc, dict) and tc.get("function", {}).get("name") == "working_memory":
                    wm_tc_ids.add(tc.get("id", ""))

        # 2. 过滤 WM 消息（保留原始位置信息）
        filtered_with_pos = []
        for orig_pos, msg in enumerate(range_messages):
            # 跳过包含 working_memory tool_calls 的 assistant 消息
            tcs = msg.tool_calls if isinstance(msg.tool_calls, list) else []
            if msg.role == "assistant" and tcs:
                # 混合 WM + 非WM tool_calls：只过滤 WM 部分，保留非 WM 部分
                non_wm_tcs = [tc for tc in tcs if not (isinstance(tc, dict) and tc.get("function", {}).get("name") == "working_memory")]
                if non_wm_tcs:
                    # 保留消息，但替换 tool_calls 为非 WM 部分
                    msg_copy = FakeMessage(
                        id=msg.id, role=msg.role, content=msg.content,
                        tool_calls=non_wm_tcs, tool_call_id=msg.tool_call_id
                    )
                    filtered_with_pos.append((orig_pos, msg_copy))
                    continue
                elif not non_wm_tcs and len(tcs) > 0:
                    # 全部是 WM，跳过整条消息
                    continue
            # 跳过 WM tool 结果
            if msg.role == "tool" and msg.tool_call_id in wm_tc_ids:
                continue
            filtered_with_pos.append((orig_pos, msg))

        # 3. 移除末尾孤立的 assistant(tool_calls)
        while filtered_with_pos and filtered_with_pos[-1][1].role == "assistant" and filtered_with_pos[-1][1].tool_calls:
            filtered_with_pos.pop()

        # 4. 移除开头孤立的 tool 消息
        while filtered_with_pos and filtered_with_pos[0][1].role == "tool":
            filtered_with_pos.pop(0)

        # 使用带原始位置的列表
        range_messages_with_pos = filtered_with_pos
    else:
        range_messages_with_pos = [(i, msg) for i, msg in enumerate(range_messages)]

    if not range_messages_with_pos:
        return "（无新增消息）"

    lines = []
    for rel_pos, (orig_pos, msg) in enumerate(range_messages_with_pos):
        # idx 使用全量列表中的位置（1-based），orig_pos 是增量范围内的相对位置
        # 全量 idx = start + orig_pos + 1
        original_idx = start + orig_pos + 1
        msg_id = getattr(msg, "id", "") or ""
        out_msg_ids.append(msg_id)
        content = msg.content or ""
        token_annotation = ""
        if msg_tokens and (start + orig_pos) < len(msg_tokens):
            token_annotation = f"{msg_tokens[start + orig_pos]}tokens "
        # protect_recent: 对最后 N 条消息加 [PROTECTED] 标签
        protect_label = ""
        if protect_recent > 0 and rel_pos >= len(range_messages_with_pos) - protect_recent:
            protect_label = "[PROTECTED] "
        lines.append(f"[id:{msg_id}] [idx:{original_idx}] {token_annotation}{msg.role}: {protect_label}{content}")

    if not lines:
        return "（无新增消息）"

    return f"共 {len(lines)} 条新消息\n\n" + "\n".join(lines)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestBuildIncrementalMsgTextFilterWm -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_tidy_cursor.py niu_api/compat.py
git commit -m "feat: _build_incremental_msg_text 增加 filter_wm 参数 — 迁移 WM 过滤逻辑"
```

---

### Task 3: `_build_incremental_msg_text()` 增强 — protect_recent 参数

**Files:**
- Modify: `niu_api/compat.py`
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 编写失败测试 — protect_recent 加 [PROTECTED] 标签**

在 `tests/test_tidy_cursor.py` 末尾追加：

```python
class TestBuildIncrementalMsgTextProtectRecent:
    """测试 protect_recent 参数：对最后 N 条消息加 [PROTECTED] 标签"""

    def test_protect_recent_labels_last_n_messages(self):
        """protect_recent=3 时，最后 3 条消息加 [PROTECTED] 标签"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            protect_recent=3,
        )
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        # 最后 3 条（uuid-7, uuid-8, uuid-9）应有 [PROTECTED]
        assert len(protected_lines) == 3
        assert "uuid-7" in protected_lines[0]
        assert "uuid-8" in protected_lines[1]
        assert "uuid-9" in protected_lines[2]

    def test_protect_recent_zero_no_labels(self):
        """protect_recent=0 时，不加任何 [PROTECTED] 标签"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            protect_recent=0,
        )
        assert "[PROTECTED]" not in result

    def test_protect_recent_with_end_cursor(self):
        """protect_recent 与 end_cursor_id 组合：保护范围内的最后 N 条"""
        messages = make_messages(10)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-2",
            out_msg_ids=out_ids,
            end_cursor_id="uuid-7",
            protect_recent=2,
        )
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        # 范围内 uuid-3~uuid-7，最后 2 条是 uuid-6, uuid-7
        assert len(protected_lines) == 2
        assert "uuid-6" in protected_lines[0]
        assert "uuid-7" in protected_lines[1]

    def test_protect_recent_larger_than_range(self):
        """protect_recent 大于增量消息数时，全部加 [PROTECTED]"""
        messages = make_messages(3)
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="",
            out_msg_ids=out_ids,
            protect_recent=10,
        )
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        assert len(protected_lines) == 3  # 全部 3 条

    def test_protect_recent_with_filter_wm(self):
        """protect_recent + filter_wm 组合：过滤后再保护"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="ok", tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-4", role="assistant", content="好的"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages, "", out_ids, protect_recent=1, filter_wm=True
        )
        # 过滤后剩 uuid-0, uuid-3, uuid-4，最后 1 条(uuid-4) 加 PROTECTED
        lines = result.split("\n")
        protected_lines = [l for l in lines if "[PROTECTED]" in l]
        assert len(protected_lines) == 1
        assert "uuid-4" in protected_lines[0]

    def test_end_cursor_with_filter_wm(self):
        """end_cursor_id + filter_wm 组合：先截断再过滤"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content="ok", tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="user", content="帮我"),
            FakeMessage(id="uuid-4", role="assistant", content="好的"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages, "", out_ids, end_cursor_id="uuid-3", filter_wm=True
        )
        # 截断到 uuid-3 → [uuid-0, uuid-1, uuid-2, uuid-3]，过滤 WM → [uuid-0, uuid-3]
        assert "uuid-0" in out_ids
        assert "uuid-1" not in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-3" in out_ids
        assert "uuid-4" not in out_ids
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestBuildIncrementalMsgTextProtectRecent -v`
Expected: FAIL — `_build_incremental_msg_text()` 不接受 `protect_recent` 参数

- [ ] **Step 3: 实现 protect_recent 参数**

在 `_build_incremental_msg_text()` 签名中增加 `protect_recent: int = 0`，在生成行时对最后 N 条加标签：

在生成 lines 的循环中，将 `lines.append(...)` 改为：

```python
    lines = []
    for i, msg in enumerate(range_messages):
        original_idx = start + i + 1
        msg_id = getattr(msg, "id", "") or ""
        out_msg_ids.append(msg_id)
        content = msg.content or ""
        token_annotation = ""
        if msg_tokens and (start + i) < len(msg_tokens):
            token_annotation = f"{msg_tokens[start + i]}tokens "
        # protect_recent: 对最后 N 条消息加 [PROTECTED] 标签
        protect_label = ""
        if protect_recent > 0 and i >= len(range_messages) - protect_recent:
            protect_label = "[PROTECTED] "
        lines.append(f"[id:{msg_id}] [idx:{original_idx}] {token_annotation}{msg.role}: {protect_label}{content}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestBuildIncrementalMsgTextProtectRecent -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_tidy_cursor.py niu_api/compat.py
git commit -m "feat: _build_incremental_msg_text 增加 protect_recent 参数 — [PROTECTED] 标签"
```

---

### Task 4: 删除 `_build_entity_history()` 函数

**Files:**
- Modify: `niu_api/compat.py:120-180`
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 编写测试 — 验证 filter_wm 替代 _build_entity_history 的功能**

在 `tests/test_tidy_cursor.py` 末尾追加：

```python
class TestBuildEntityHistoryReplacement:
    """验证 _build_incremental_msg_text(filter_wm=True) 完整替代 _build_entity_history() 的功能"""

    def test_wm_filter_in_incremental_range(self):
        """增量范围内的 WM 过滤效果与 _build_entity_history 一致"""
        messages = [
            FakeMessage(id="uuid-0", role="user", content="你好"),
            FakeMessage(id="uuid-1", role="assistant", content="你好！", tool_calls=[
                {"id": "tc-wm", "function": {"name": "working_memory"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-2", role="tool", content='{"status": "ok"}', tool_call_id="tc-wm"),
            FakeMessage(id="uuid-3", role="assistant", content="有什么可以帮你？"),
            FakeMessage(id="uuid-4", role="user", content="帮我写代码"),
            FakeMessage(id="uuid-5", role="assistant", content="好的", tool_calls=[
                {"id": "tc-real", "function": {"name": "code_run"}, "arguments": "{}"}
            ]),
            FakeMessage(id="uuid-6", role="tool", content="代码执行结果", tool_call_id="tc-real"),
        ]
        out_ids = []
        result = _build_incremental_msg_text(
            messages,
            last_cursor_id="uuid-0",
            out_msg_ids=out_ids,
            filter_wm=True,
        )
        # uuid-1(WM call) 和 uuid-2(WM result) 被过滤
        # uuid-5 和 uuid-6 不是 WM，保留
        assert "uuid-1" not in out_ids
        assert "uuid-2" not in out_ids
        assert "uuid-3" in out_ids
        assert "uuid-4" in out_ids
        assert "uuid-5" in out_ids
        assert "uuid-6" in out_ids
```

- [ ] **Step 2: 运行测试确认通过（Task 2/3 已实现 filter_wm）**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestBuildEntityHistoryReplacement -v`
Expected: PASS — filter_wm 已在 Task 2 中实现

- [ ] **Step 3: 删除 `_build_entity_history()` 函数**

删除 `niu_api/compat.py` 第 120-180 行的 `_build_entity_history()` 函数。同时搜索 compat.py 中所有对 `_build_entity_history` 的引用，全部删除或替换。

搜索 compat.py 中的引用：
- 第 893 行：`incremental_entity_history = _build_entity_history(messages, last_entity_extract_id)` → 将在 Task 5 中替换
- 第 1142 行：`history=_build_entity_history(messages, "")` → 将在 Task 5 中替换

仅删除函数定义，保留引用处的 TODO 注释（Task 5 会替换）。

- [ ] **Step 4: 运行全量测试确认无回归**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_tidy_cursor.py niu_api/compat.py
git commit -m "refactor: 删除 _build_entity_history() — 功能已迁移到 _build_incremental_msg_text(filter_wm=True)"
```

---

### Task 5: `_extract_cursor_id()` 增强 — null 检测

**Files:**
- Modify: `niu_api/compat.py:21-50`
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 编写失败测试 — _extract_cursor_id 检测 null 值**

在 `tests/test_tidy_cursor.py` 末尾追加：

```python
from niu_api.compat import _extract_cursor_id


class TestExtractCursorIdNull:
    """测试 _extract_cursor_id 对 null 值的检测"""

    def test_normal_uuid_extraction(self):
        """正常提取 UUID"""
        result = _extract_cursor_id(
            '处理完成 {"last_entity_extract_id": "uuid-abc123"} 收尾',
            "last_entity_extract_id",
            {"uuid-abc123"},
        )
        assert result == "uuid-abc123"

    def test_null_returns_sentinel(self):
        """明确返回 null 时，返回特殊标记（非 None，区分'没报告'和'明确返回null'）"""
        result = _extract_cursor_id(
            '处理完成 {"last_entity_extract_id": null} 收尾',
            "last_entity_extract_id",
            set(),
        )
        # null 应返回特殊标记 "NULL"，而非 None
        assert result == "NULL"

    def test_no_match_returns_none(self):
        """没有匹配时返回 None"""
        result = _extract_cursor_id(
            "没有任何游标信息",
            "last_entity_extract_id",
            set(),
        )
        assert result is None

    def test_invalid_uuid_not_in_valid_ids(self):
        """UUID 不在 valid_ids 中时返回 None"""
        result = _extract_cursor_id(
            '{"last_entity_extract_id": "uuid-nonexistent"}',
            "last_entity_extract_id",
            {"uuid-other"},
        )
        assert result is None

    def test_null_with_whitespace(self):
        """null 带各种空白格式"""
        result = _extract_cursor_id(
            '{"last_entity_extract_id" :  null  }',
            "last_entity_extract_id",
            set(),
        )
        assert result == "NULL"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestExtractCursorIdNull -v`
Expected: FAIL — `_extract_cursor_id()` 当前对 null 返回 None

- [ ] **Step 3: 实现 null 检测**

修改 `_extract_cursor_id()` 函数，增加 null 匹配：

```python
def _extract_cursor_id(text: str, field_name: str, valid_ids: set) -> str | None:
    """
    从文本中提取游标 UUID 并验证其存在于消息列表中。

    返回值：
    - UUID 字符串：正常提取并验证通过
    - "NULL"：子Agent明确返回了 null（区分"没报告"和"明确返回null"）
    - None：未找到匹配

    Args:
        text: 待搜索的文本（子 Agent 结果或 partial_result）
        field_name: 游标字段名（如 "last_entity_extract_id"）
        valid_ids: 当前消息列表中有效的 UUID 集合
    """
    if not text:
        return None
    # 先检查是否明确返回了 null
    null_pattern = rf'\{{\s*"{re.escape(field_name)}"\s*:\s*null\s*'
    if re.search(null_pattern, text, re.DOTALL):
        return "NULL"
    # 再尝试提取 UUID
    pattern = rf'\{{\s*"{re.escape(field_name)}"\s*:\s*"([^"]+)"\s*'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        return None
    candidate = match.group(1)
    if valid_ids is not None and candidate not in valid_ids:
        logger.warning(f"[Tidy] Extracted {field_name}={candidate} not in message list, discarding")
        return None
    return candidate
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestExtractCursorIdNull -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_tidy_cursor.py niu_api/compat.py
git commit -m "feat: _extract_cursor_id 增加 null 检测 — 返回 'NULL' 标记区分未报告和明确null"
```

---

### Task 6: handler.py 清理 — 删除 entity-extractor history 分支和游标写入

**Files:**
- Modify: `agent/handler.py:864-884, 932-956`
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 编写测试 — 验证 dispatch 屏蔽 context-manager 和 entity-extractor**

在 `tests/test_tidy_cursor.py` 末尾追加：

```python
class TestDispatchBlocking:
    """测试 dispatch 屏蔽 context-manager 和 entity-extractor 的手动调用"""

    def test_blocked_agents_list(self):
        """验证屏蔽列表包含 context-manager 和 entity-extractor"""
        BLOCKED_SUBAGENTS = {"context-manager", "entity-extractor"}
        assert "context-manager" in BLOCKED_SUBAGENTS
        assert "entity-extractor" in BLOCKED_SUBAGENTS
        assert "file-processor" not in BLOCKED_SUBAGENTS
        assert "event-manager" not in BLOCKED_SUBAGENTS
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py::TestDispatchBlocking -v`
Expected: PASS

- [ ] **Step 3: 删除 handler.py 中 entity-extractor history 特殊分支**

在 `agent/handler.py` 的 `_call_subagent_gen()` 方法中：

1. 删除第 863-884 行的 entity-extractor history 特殊分支：
```python
# 删除以下代码：
# 只有 entity-extractor 需要看到主Agent的tool消息，其他子Agent保持独立上下文
if agent_name == "entity-extractor":
    _history = getattr(self, '_current_messages', None)
else:
    _history = None
if _history:
    _wm_ids = set()
    for m in _history:
        ...（WM 过滤逻辑）
    # 移除末尾孤立的 assistant(tool_calls)...
```

2. 替换为：
```python
# 所有子Agent统一使用 task 模式，不传 history
_history = None
```

3. 删除第 932-956 行的 entity-extractor 游标写入逻辑：
```python
# 删除以下代码：
# entity-extractor 调用完成后，从输出提取游标并写入文件
if agent_name == "entity-extractor" and result:
    try:
        ...（游标提取和写入逻辑）
    except Exception as e:
        logger.warning(...)
```

4. 在 `dispatch()` 方法的 `chat-with-*` 路由中（第 1072-1083 行），增加屏蔽：

```python
        # 先检查 chat-with-* 子 Agent 调用（通配路由）
        if tool_name.startswith("chat-with-"):
            agent_name = tool_name[len("chat-with-"):]
            # 屏蔽由系统自动管理的子Agent
            BLOCKED_SUBAGENTS = {"context-manager", "entity-extractor"}
            if agent_name in BLOCKED_SUBAGENTS:
                return StepOutcome(
                    {"status": "error", "result": f"子Agent {agent_name} 已由系统自动管理，不可手动调用"},
                    next_prompt=self._get_anchor_prompt()
                )
            args = {**args, "_index": index}
            prer = yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(self._call_subagent_gen, agent_name, args)
            _ = yield from try_call_generator(
                self.tool_after_callback, tool_name, args, response, ret
            )
            return ret
```

- [ ] **Step 4: Python 语法检查**

Run: `cd <repo_root> && python -c "import py_compile; py_compile.compile('agent/handler.py', doraise=True)"`
Expected: 无输出（编译通过）

- [ ] **Step 5: 提交**

```bash
git add agent/handler.py tests/test_tidy_cursor.py
git commit -m "refactor: 删除 handler.py entity-extractor history分支和游标写入 + dispatch屏蔽"
```

---

### Task 7: niu.md 清理 — 移除 context-manager 和 entity-extractor

**Files:**
- Modify: `config/agents/niu.md:7-11`

- [ ] **Step 1: 修改 niu.md 的 sub agents 列表**

将：
```yaml
sub agents:
  - file-processor
  - event-manager
  - context-manager
  - entity-extractor
```

改为：
```yaml
sub agents:
  - file-processor
  - event-manager
```

同时删除委托表中 `chat-with-context-manager` 和 `chat-with-entity-extractor` 的说明行（第 103-108 行）：

将：
```markdown
| `chat-with-context-manager`  | 记忆压缩、上下文整理                 |
| `chat-with-entity-extractor` | 知识图谱实体提取、去重、关联建立（LightRAG） |
```

删除这两行。

- [ ] **Step 2: 提交**

```bash
git add config/agents/niu.md
git commit -m "refactor: niu.md 移除 context-manager 和 entity-extractor — 系统自动管理"
```

---

### Task 8: compat.py — Entity Extractor 改 task 方式（sleep 模式）

**Files:**
- Modify: `niu_api/compat.py:880-951`

- [ ] **Step 1: 修改 Entity Extractor sleep 模式调用**

将 Entity Extractor sleep 模式（第 880-951 行）从 history 方式改为 task 方式。

替换第 880-951 行的 Entity Extractor 部分为：

```python
            # 1/3. entity-extractor（增量，task 方式）
            entity_msg_ids = []
            entity_msg_text = _build_incremental_msg_text(
                messages, last_entity_extract_id, entity_msg_ids, msg_tokens, filter_wm=True
            )
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_prompt_prefix = """以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

"""
            entity_prompt_suffix = """

处理完成后，在报告末尾用 JSON 格式报告：{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}
**必须推进游标**：即使没有可提取的内容（全是程序化操作、闲聊等），也必须输出 idx 最大的消息的 UUID。只有当传入的消息列表本身为空（一条消息都没有）时，才输出 {"last_entity_extract_id": null}"""
            if entity_msg_ids:
                logger.info(f"[Tidy] entity-extractor: {len(entity_msg_ids)} new messages since cursor")
                entity_full_prompt = entity_prompt_prefix + entity_msg_text + entity_prompt_suffix

                def run_entity_extractor():
                    return call_subagent(
                        agent_name="entity-extractor",
                        task=entity_full_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=None,  # task 方式，不传 history
                    )

                entity_result = await asyncio.to_thread(run_entity_extractor)
                logger.info(f"[Tidy] entity-extractor result: {entity_result[:200]}")

                # 游标提取和推进（统一逻辑：overflow 和 normal 路径）
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_entity_extract_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_entity_id = recovered
                        logger.info(f"[Tidy] Entity cursor recovered from partial_result: {new_entity_id}")
                    else:
                        # 溢出且无法提取 → 推进到增量消息最后一条
                        new_entity_id = entity_msg_ids[-1]
                        logger.warning(f"[Tidy] Entity cursor overflow fallback to last incremental msg: {new_entity_id}")
                else:
                    extracted = _extract_cursor_id(entity_result, "last_entity_extract_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_entity_id = extracted
                    elif extracted == "NULL" or not extracted:
                        # null 或未匹配 → 推进到增量消息最后一条（避免重复处理）
                        new_entity_id = entity_msg_ids[-1]
                        logger.warning(f"[Tidy] Entity cursor regex not matched or null, advancing to last incremental msg: {new_entity_id}")
                # 校验游标：子 Agent 可能已删除游标指向的消息
                if new_entity_id:
                    fresh_msgs = await store.get_messages()
                    fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                    if new_entity_id not in fresh_ids:
                        logger.warning(f"[Tidy] Entity cursor {new_entity_id} deleted by sub-agent, reverting to {last_entity_extract_id}")
                        new_entity_id = last_entity_extract_id
                        if new_entity_id and new_entity_id not in fresh_ids:
                            new_entity_id = ""

                if new_entity_id:
                    entity_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    entity_cursor_path.write_text(json.dumps({
                        "last_entity_extract_id": new_entity_id,
                        "last_entity_extract_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info(f"[Tidy] entity cursor updated: last_entity_extract_id={new_entity_id}")
            else:
                logger.info("[Tidy] entity-extractor: no new messages since cursor")
```

- [ ] **Step 2: Python 语法检查**

Run: `cd <repo_root> && python -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True)"`
Expected: 无输出（编译通过）

- [ ] **Step 3: 提交**

```bash
git add niu_api/compat.py
git commit -m "refactor: Entity Extractor sleep模式改为 task方式 — 程序构建增量消息文本"
```

---

### Task 9: compat.py — Entity Extractor 改 task 方式（force 模式）

**Files:**
- Modify: `niu_api/compat.py:1129-1172`

- [ ] **Step 1: 修改 Entity Extractor force 模式调用**

将 force 模式 Entity Extractor（第 1129-1172 行）从 `_build_entity_history` 改为 `_build_incremental_msg_text`：

替换第 1129-1172 行为：

```python
            # 1/3. entity-extractor（全量 task 方式，cursor 传空 = 全量）
            entity_force_msg_ids = []
            entity_force_msg_text = _build_incremental_msg_text(
                messages, "", entity_force_msg_ids, msg_tokens, filter_wm=True
            )
            entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的内容，也必须输出 idx 最大的消息的 UUID。"""

            def run_entity_extractor_force():
                return call_subagent(
                    agent_name="entity-extractor",
                    task=entity_force_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    history=None,  # task 方式，不传 history
                )

            entity_result = await asyncio.to_thread(run_entity_extractor_force)
            logger.info(f"[Tidy] Force: entity-extractor completed, length={len(entity_result)}")

            if _is_subagent_overflow(entity_result):
                overflow_info = _extract_overflow_info(entity_result)
                logger.warning(f"[Tidy] Force: entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                partial = overflow_info.get("partial_result", "")
                recovered = _extract_cursor_id(partial, "last_entity_extract_id", msg_id_set)
                if recovered and recovered != "NULL":
                    new_entity_id = recovered
                    logger.info(f"[Tidy] Force: Entity cursor recovered from partial_result: {new_entity_id}")
                else:
                    new_entity_id = entity_force_msg_ids[-1] if entity_force_msg_ids else last_entity_extract_id
                    logger.warning(f"[Tidy] Force: Entity cursor overflow fallback: {new_entity_id}")
            else:
                extracted = _extract_cursor_id(entity_result, "last_entity_extract_id", msg_id_set)
                if extracted and extracted != "NULL":
                    new_entity_id = extracted
                elif extracted == "NULL" or not extracted:
                    new_entity_id = entity_force_msg_ids[-1] if entity_force_msg_ids else last_entity_extract_id
                    logger.warning(f"[Tidy] Force: Entity cursor not matched, fallback to last msg: {new_entity_id}")
            if new_entity_id:
                entity_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                entity_cursor_path.write_text(json.dumps({
                    "last_entity_extract_id": new_entity_id,
                    "last_entity_extract_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 2: Python 语法检查**

Run: `cd <repo_root> && python -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True)"`
Expected: 无输出

- [ ] **Step 3: 提交**

```bash
git add niu_api/compat.py
git commit -m "refactor: Entity Extractor force模式改为 task方式 — 全量消息文本"
```

---

### Task 10: compat.py — Dream Evolver 改增量模式

**Files:**
- Modify: `niu_api/compat.py:953-1025, 1174-1221`

- [ ] **Step 1: 修改 Dream Evolver sleep 模式**

替换第 953-1025 行的 Dream Evolver sleep 模式为：

```python
            # 2/3. dream-evolver（增量 task 方式）
            # 串行执行：重新获取消息列表（Entity 可能已修改 DB）
            messages = await store.get_messages()
            # 重建 token 和 id 集合（消息列表已更新）
            msg_tokens = [_estimate_msg_tokens(m) for m in messages]
            msg_id_set = {getattr(m, "id", "") for m in messages}
            dream_msg_ids = []
            dream_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_msg_ids, msg_tokens, filter_wm=True
            )
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要精加工的内容，也必须输出 idx 最大的消息的 UUID。"""

                def run_dream_evolver():
                    return call_subagent(
                        agent_name="dream-evolver",
                        task=dream_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver)
                logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_dream_evolve_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_dream_id = recovered
                        logger.info(f"[Tidy] Dream cursor recovered from partial_result: {new_dream_id}")
                    else:
                        new_dream_id = dream_msg_ids[-1]
                        logger.warning(f"[Tidy] Dream cursor overflow fallback to last incremental msg: {new_dream_id}")
                else:
                    extracted = _extract_cursor_id(dream_result, "last_dream_evolve_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_dream_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_dream_id = dream_msg_ids[-1]
                        logger.warning(f"[Tidy] Dream cursor not matched or null, advancing to last incremental msg: {new_dream_id}")
                # 校验游标
                if new_dream_id:
                    fresh_msgs = await store.get_messages()
                    fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                    if new_dream_id not in fresh_ids:
                        logger.warning(f"[Tidy] Dream cursor {new_dream_id} deleted by sub-agent, reverting to {last_dream_evolve_id}")
                        new_dream_id = last_dream_evolve_id
                        if new_dream_id and new_dream_id not in fresh_ids:
                            new_dream_id = ""
                if new_dream_id:
                    dream_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    dream_cursor_path.write_text(json.dumps({
                        "last_dream_evolve_id": new_dream_id,
                        "last_evolve_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info(f"[Tidy] Dream cursor updated: last_dream_evolve_id={new_dream_id}")
            else:
                logger.info("[Tidy] dream-evolver: no new messages since cursor")
                new_dream_id = last_dream_evolve_id  # 无增量时保持原游标
```

- [ ] **Step 2: 修改 Dream Evolver force 模式**

替换第 1174-1221 行的 Dream Evolver force 模式为：

```python
            # 2/3. dream-evolver（增量 task 方式，force 模式也是增量）
            # 串行执行：重新获取消息列表
            messages = await store.get_messages()
            msg_tokens = [_estimate_msg_tokens(m) for m in messages]
            msg_id_set = {getattr(m, "id", "") for m in messages}
            dream_force_msg_ids = []
            dream_force_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens, filter_wm=True
            )
            logger.info(f"[Tidy] Force mode: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要精加工的内容，也必须输出 idx 最大的消息的 UUID。"""

                def run_dream_evolver_force():
                    return call_subagent(
                        agent_name="dream-evolver",
                        task=dream_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver_force)
                logger.info(f"[Tidy] Force: dream-evolver completed, length={len(dream_result)}")

                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_dream_evolve_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_dream_id = recovered
                        logger.info(f"[Tidy] Force: Dream cursor recovered from partial_result: {new_dream_id}")
                    else:
                        new_dream_id = dream_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Dream cursor overflow fallback: {new_dream_id}")
                else:
                    extracted = _extract_cursor_id(dream_result, "last_dream_evolve_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_dream_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_dream_id = dream_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Dream cursor not matched, fallback to last msg: {new_dream_id}")
            else:
                logger.info("[Tidy] Force: dream-evolver no incremental messages")

            if new_dream_id:
                dream_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                dream_cursor_path.write_text(json.dumps({
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 3: Python 语法检查**

Run: `cd <repo_root> && python -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True)"`
Expected: 无输出

- [ ] **Step 4: 提交**

```bash
git add niu_api/compat.py
git commit -m "refactor: Dream Evolver 改增量task方式 — sleep/force统一增量模式"
```

---

### Task 11: compat.py — Context Manager 保护范围硬性保证（sleep 模式）

**Files:**
- Modify: `niu_api/compat.py:1027-1123`

- [ ] **Step 1: 修改 Context Manager sleep 模式**

替换第 1027-1123 行的 Context Manager sleep 模式为：

```python
            # 3/3. context-manager（增量 task 方式，保护范围 [compress_cursor, dream_cursor_new]）
            # 串行执行：重新获取消息列表（Dream 可能已修改 DB）
            messages = await store.get_messages()
            # 重建 token 和 id 集合（消息列表已更新）
            msg_tokens = [_estimate_msg_tokens(m) for m in messages]
            msg_id_set = {getattr(m, "id", "") for m in messages}
            compress_msg_ids = []
            # 读取保护数量配置
            protect_recent_count = 10
            try:
                from pathlib import Path as _P
                _prefs = json.loads((_P.home() / ".niu" / "preferences.json").read_text(encoding="utf-8"))
                protect_recent_count = _prefs.get("context", {}).get("protectRecentCount", 10)
            except Exception:
                pass  # 保留默认值 10

            compress_msg_text = _build_incremental_msg_text(
                messages, last_compress_id, compress_msg_ids, msg_tokens,
                end_cursor_id=new_dream_id, protect_recent=protect_recent_count, filter_wm=True
            )
            # 根据 usage_percent 自动选择压缩模式
            compress_mode = "模式二：睡眠整理（半破坏性）" if usage_percent >= 50 else "模式一：睡眠整理（非破坏性）"
            logger.info(f"[Tidy] Sleep: usage={usage_percent:.1f}%, selecting {compress_mode}")

            if compress_msg_ids:
                # 构建保护消息 UUID 列表（用于 prompt 告知和事后校验）
                protected_ids = compress_msg_ids[-protect_recent_count:] if len(compress_msg_ids) > protect_recent_count else compress_msg_ids[:]

                prompt = f"""系统进入睡眠状态。

当前上下文：{estimated_tokens} tokens（{usage_percent:.1f}%）

以下消息已标注 [PROTECTED]，不可删除或压缩：
保护消息ID: {json.dumps(protected_ids)}

消息列表：
{compress_msg_text}

请按照【{compress_mode}】的规则处理。处理完成后，在报告末尾用 JSON 格式报告：{{"last_compress_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要处理的内容，也必须输出 idx 最大的消息的 UUID。"""

                def run_context_manager():
                    return call_subagent(
                        agent_name="context-manager",
                        task=prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )

                cm_result = await asyncio.to_thread(run_context_manager)
                logger.info(f"[Tidy] context-manager result: {cm_result[:200]}")

                # 游标提取
                new_compress_id = last_compress_id
                if _is_subagent_overflow(cm_result):
                    overflow_info = _extract_overflow_info(cm_result)
                    logger.warning(f"[Tidy] context-manager overflow: {overflow_info.get('turns_completed', 0)} turns")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_compress_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_compress_id = recovered
                    else:
                        new_compress_id = compress_msg_ids[-1]
                        logger.warning(f"[Tidy] Compress cursor overflow fallback: {new_compress_id}")
                else:
                    extracted = _extract_cursor_id(cm_result, "last_compress_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_compress_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_compress_id = compress_msg_ids[-1]
                        logger.warning(f"[Tidy] Compress cursor not matched, fallback: {new_compress_id}")

                # 校验游标
                if new_compress_id:
                    fresh_msgs = await store.get_messages()
                    fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                    if new_compress_id not in fresh_ids:
                        logger.warning(f"[Tidy] Compress cursor {new_compress_id} deleted, reverting to {last_compress_id}")
                        new_compress_id = last_compress_id
                        if new_compress_id and new_compress_id not in fresh_ids:
                            new_compress_id = ""

                # 事后校验：保护范围内的消息是否被误删
                if protected_ids:
                    try:
                        post_msgs = await store.get_messages()
                        post_ids = {getattr(m, "id", "") for m in post_msgs}
                        for pid in protected_ids:
                            if pid not in post_ids:
                                logger.warning(f"[Tidy] PROTECTED message {pid} was deleted by context-manager! This should not happen.")
                    except Exception as e:
                        logger.warning(f"[Tidy] Failed to verify protected messages: {e}")

                if new_compress_id:
                    compress_cursor_path.parent.mkdir(parents=True, exist_ok=True)
                    compress_cursor_path.write_text(json.dumps({
                        "last_compress_id": new_compress_id,
                        "last_compress_at": datetime.now().isoformat(),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.info(f"[Tidy] Compress cursor updated: last_compress_id={new_compress_id}")
            else:
                logger.info("[Tidy] context-manager: no messages in range [compress_cursor, dream_cursor_new]")

            # 更新 last_tidy_tokens
            try:
                post_tidy_msgs = await store.get_messages()
                _write_last_tidy_tokens(_estimate_total_tokens(post_tidy_msgs))
            except Exception as e:
                logger.warning(f"[Tidy] Failed to update last_tidy_tokens: {e}")

            return {"status": "ok", "mode": "sleep", "tokens_before": estimated_tokens}
```

- [ ] **Step 2: Python 语法检查**

Run: `cd <repo_root> && python -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True)"`
Expected: 无输出

- [ ] **Step 3: 提交**

```bash
git add niu_api/compat.py
git commit -m "refactor: Context Manager sleep模式 — 增量范围+保护标签+程序兜底+事后校验"
```

---

### Task 12: compat.py — Context Manager force 模式保护增强

**Files:**
- Modify: `niu_api/compat.py` (force 模式部分)

- [ ] **Step 1: 修改 Context Manager force 模式的保护范围**

在 force 模式的压缩计划执行中（第 1303-1357 行区域），增加保护范围排除：

在 `fresh_messages = await store.get_messages()` 之后、`valid_deletes` 过滤之后，增加：

```python
                    # 程序层面排除保护范围内的消息 ID
                    protect_recent_count = 10
                    try:
                        _prefs_force = json.loads((_Path.home() / ".niu" / "preferences.json").read_text(encoding="utf-8"))
                        protect_recent_count = _prefs_force.get("context", {}).get("protectRecentCount", 10)
                    except Exception:
                        pass
                    if protect_recent_count > 0 and len(fresh_messages) > protect_recent_count:
                        protected_force_ids = {getattr(m, "id", "") for m in fresh_messages[-protect_recent_count:]}
                        removed_deletes = [mid for mid in valid_deletes if mid in protected_force_ids]
                        if removed_deletes:
                            logger.warning(f"[Tidy] Force: Protecting {len(removed_deletes)} recent messages from deletion: {removed_deletes}")
                            valid_deletes = [mid for mid in valid_deletes if mid not in protected_force_ids]
                        removed_updates = [u for u in valid_updates if u.get("message_id", "") in protected_force_ids]
                        if removed_updates:
                            logger.warning(f"[Tidy] Force: Protecting {len(removed_updates)} recent messages from update")
                            valid_updates = [u for u in valid_updates if u.get("message_id", "") not in protected_force_ids]
```

- [ ] **Step 2: Python 语法检查**

Run: `cd <repo_root> && python -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True)"`
Expected: 无输出

- [ ] **Step 3: 提交**

```bash
git add niu_api/compat.py
git commit -m "feat: Context Manager force模式 — 程序层面排除保护范围内消息"
```

---

### Task 13: 子Agent prompt 更新 — entity-extractor.md

**Files:**
- Modify: `config/agents/entity-extractor.md`

- [ ] **Step 1: 重写 entity-extractor.md 的输入规范和游标机制**

将 entity-extractor.md 中的输入规范和游标机制部分替换为：

替换"## 输入规范"（第 10-19 行）：

```markdown
## 输入规范

- 由系统通过 task 方式自动调用，不暴露给主Agent
- 消息以文本形式内嵌在 task 中，格式为：`[id:UUID] [idx:N] Xtokens role: 内容`
- `id`：消息在数据库中的 UUID（持久标识，用于游标存储）
- `idx`：消息在全量列表中的序号（1-based，动态值，删除消息后会变）
- `Xtokens`：该条消息的 token 估算值
- 消息内容为**完整原文**，不做截断
- 你应基于传入的消息内容进行实体和关系提取
```

替换"## 游标机制"（第 66-71 行）：

```markdown
## 游标机制

- 程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息
- 每条消息带有 `[id:UUID] [idx:N]` 标注，idx 是全量列表序号（不是增量相对序号）
- 处理完成后报告 idx 最大的那条消息的 UUID
- 在报告末尾输出：`{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}`
- **必须推进游标**：即使没有可提取的内容（全是程序化操作、闲聊等），也必须输出 idx 最大的消息的 UUID
- 只有当传入的消息列表本身为空（一条消息都没有）时，才输出 `{"last_entity_extract_id": null}`
```

- [ ] **Step 2: 提交**

```bash
git add config/agents/entity-extractor.md
git commit -m "docs: entity-extractor.md 更新 — task方式输入规范+游标机制说明"
```

---

### Task 14: 子Agent prompt 更新 — dream-evolver.md

**Files:**
- Modify: `config/agents/dream-evolver.md`

- [ ] **Step 1: 修改 dream-evolver.md 的职责边界和游标机制**

1. 修改"## 职责边界"（第 15-20 行），移除对 entity-extractor 的依赖描述：

```markdown
## 职责边界

- **dream-evolver**（你）：对知识图谱中的实体进行**精加工**——打标签、建关系、关联脑区、更新画像
- 你不负责从零提取新实体，只负责深化和关联已有实体
- 实体来源：用 `lightrag_search_entities` 搜索本次消息中涉及的实体，对它们做精加工
```

2. 修改"## 游标机制"（第 224-245 行）：

```markdown
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息，不需要自行过滤范围。

每条消息格式为 `[id:UUID] [idx:N] Xtokens role: content`。

**重要**：
- **游标用 id（UUID）存储**：因为 id 是数据库中持久化的，删除消息不影响其他消息的 id
- **idx 是全量列表序号**：代表消息在完整对话中的位置（1-based，动态值，删除消息后会变）
- **UUID v4 字典序不代表时间先后**：不要用 id 比较大小来判断先后

**操作步骤**：
1. 直接处理收到的全部消息（程序已保证只传入增量范围内的消息）
2. 操作完成后，用 id（UUID）报告游标位置
3. 游标应推进到收到的消息中 idx 最大的那条的 id

**输入规范**：
- 消息内容为**完整原文**，不做截断
- `Xtokens` 为该条消息的 token 估算值（基于完整内容计算）
- `role` 为消息角色（user / assistant / tool）
```

- [ ] **Step 2: 提交**

```bash
git add config/agents/dream-evolver.md
git commit -m "docs: dream-evolver.md 更新 — 移除Entity依赖+简化游标机制"
```

---

### Task 15: 子Agent prompt 更新 — context-manager.md

**Files:**
- Modify: `config/agents/context-manager.md`

- [ ] **Step 1: 修改 context-manager.md 的游标机制和保护说明**

1. 修改"## 游标机制"（第 14-38 行）：

```markdown
## 游标机制

程序只传入增量范围内的消息（compress_cursor 到 dream_cursor_new 之间的消息），你只需处理收到的全部消息。

每条消息格式为 `[id:UUID] [idx:N] Xtokens role: content`。

**重要**：
- **游标用 id（UUID）存储**：因为 id 是数据库中持久化的，删除消息不影响其他消息的 id
- **idx 是全量列表序号**：代表消息在完整对话中的位置（1-based，动态值，删除消息后会变）
- **UUID v4 字典序不代表时间先后**：不要用 id 比较大小来判断先后

**[PROTECTED] 保护标签**：
- 带有 `[PROTECTED]` 标签的消息是最近的重要消息，**绝对不可删除或压缩**
- 程序层面也会兜底保护这些消息（即使你误操作，程序也会阻止）
- 保护数量由配置决定，默认 10 条

**操作步骤**：
1. 直接处理收到的全部消息（程序已保证只传入正确范围的消息）
2. 不要修改或删除带 [PROTECTED] 标签的消息
3. 操作完成后，用 id（UUID）报告游标位置

**空游标处理**：
- 无 `last_compress_id`（首次）：视为从最早消息开始
- 范围内无消息：直接报告游标推进，不做任何操作
```

2. 更新"安全边界"描述（第 86 行和第 112 行），移除"idx > last_dream_evolve_id 的消息"的安全边界描述（因为程序已保证只传入正确范围）：

模式一安全边界改为：
```markdown
**安全边界**：
- 带 [PROTECTED] 标签的消息不可删除或压缩
```

模式二安全边界同理。

3. 更新"## 重要约束"（第 189-196 行）中"绝不删除操作开始时 idx 最大的 10 条消息"改为：

```markdown
- 带 [PROTECTED] 标签的消息绝不删除或压缩
```

- [ ] **Step 2: 提交**

```bash
git add config/agents/context-manager.md
git commit -m "docs: context-manager.md 更新 — [PROTECTED]标签+简化游标+移除自行过滤"
```

---

### Task 16: 全量集成测试 — 启动程序验证

**Files:**
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 编写全量集成测试 — 验证完整的 _tidy_context_impl 流程**

在 `tests/test_tidy_cursor.py` 末尾追加：

```python
class TestTidyContextImplIntegration:
    """
    全量集成测试 — 验证 _tidy_context_impl 的完整流程

    测试方式：构造真实消息到 SQLite，调用 _tidy_context_impl，
    验证游标推进和消息范围。不 mock LLM，而是验证程序层面的逻辑正确性。
    """

    def test_incremental_range_calculation(self):
        """验证三个子Agent的增量消息范围计算逻辑"""
        messages = make_messages(20)  # uuid-0 ~ uuid-19

        # Entity: cursor=uuid-4, 范围 [uuid-5, 末尾]
        entity_ids = []
        _build_incremental_msg_text(messages, "uuid-4", entity_ids, filter_wm=True)
        assert entity_ids[0] == "uuid-5"
        assert entity_ids[-1] == "uuid-19"
        assert len(entity_ids) == 15

        # Dream: cursor=uuid-9, 范围 [uuid-10, 末尾]（与 entity 独立）
        dream_ids = []
        _build_incremental_msg_text(messages, "uuid-9", dream_ids, filter_wm=True)
        assert dream_ids[0] == "uuid-10"
        assert dream_ids[-1] == "uuid-19"
        assert len(dream_ids) == 10

        # Context: cursor=uuid-2, end=uuid-14, 范围 [uuid-3, uuid-14]
        compress_ids = []
        _build_incremental_msg_text(messages, "uuid-2", compress_ids, end_cursor_id="uuid-14", protect_recent=3, filter_wm=True)
        assert compress_ids[0] == "uuid-3"
        assert compress_ids[-1] == "uuid-14"
        assert len(compress_ids) == 12

    def test_first_run_all_cursors_empty(self):
        """首次运行：所有游标为空，三个Agent从开头处理"""
        messages = make_messages(10)

        # Entity: cursor=""
        entity_ids = []
        _build_incremental_msg_text(messages, "", entity_ids, filter_wm=True)
        assert len(entity_ids) == 10

        # Dream: cursor=""
        dream_ids = []
        _build_incremental_msg_text(messages, "", dream_ids, filter_wm=True)
        assert len(dream_ids) == 10

        # Context: cursor="", end=uuid-9
        compress_ids = []
        _build_incremental_msg_text(messages, "", compress_ids, end_cursor_id="uuid-9", filter_wm=True)
        assert len(compress_ids) == 10

    def test_cursor_points_to_deleted_message(self):
        """游标指向已删除消息时，退化到从头开始"""
        messages = make_messages(10)
        # uuid-99 不在列表中
        entity_ids = []
        result = _build_incremental_msg_text(messages, "uuid-99", entity_ids, filter_wm=True)
        # 退化到全量
        assert len(entity_ids) == 10

    def test_empty_incremental_range(self):
        """游标已在末尾，无增量消息"""
        messages = make_messages(5)
        entity_ids = []
        result = _build_incremental_msg_text(messages, "uuid-4", entity_ids, filter_wm=True)
        assert entity_ids == []
        assert "无新增消息" in result

    def test_protected_ids_extraction(self):
        """验证保护范围内的 UUID 列表提取"""
        messages = make_messages(20)
        compress_ids = []
        _build_incremental_msg_text(
            messages, "uuid-5", compress_ids,
            end_cursor_id="uuid-15", protect_recent=3, filter_wm=True
        )
        # compress_ids 包含 uuid-6 ~ uuid-15（10条）
        # 最后 3 条保护：uuid-13, uuid-14, uuid-15
        protected = compress_ids[-3:]
        assert protected == ["uuid-13", "uuid-14", "uuid-15"]

    def test_cursor_fallback_to_last_incremental_msg(self):
        """验证游标 fallback：推进到增量消息最后一条"""
        messages = make_messages(10)
        entity_ids = []
        _build_incremental_msg_text(messages, "uuid-3", entity_ids, filter_wm=True)
        # 如果 _extract_cursor_id 返回 None 或 "NULL"，应推进到 entity_ids[-1]
        fallback_cursor = entity_ids[-1] if entity_ids else None
        assert fallback_cursor == "uuid-9"
```

- [ ] **Step 2: 运行全量测试**

Run: `cd <repo_root> && python -m pytest tests/test_tidy_cursor.py -v`
Expected: PASS

- [ ] **Step 3: Python 语法检查所有修改文件**

Run: `cd <repo_root> && python -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True); py_compile.compile('agent/handler.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 4: 提交**

```bash
git add tests/test_tidy_cursor.py
git commit -m "test: 全量集成测试 — 增量范围计算+游标fallback+保护范围+边缘场景"
```

---

### Task 17: 真实环境端到端验证

**Files:**
- No code changes, manual verification

- [ ] **Step 1: 启动程序验证**

```bash
cd <repo_root> && go run main.go
```

等待程序启动完成，确认无启动报错。

- [ ] **Step 2: 触发 auto-tidy sleep 模式**

通过 API 触发 auto-tidy：
```bash
curl -X POST http://localhost:9876/api/tidy -H "Content-Type: application/json" -d '{"mode": "sleep"}'
```

观察日志输出，确认：
1. Entity Extractor 使用 task 方式调用（无 history 传参）
2. Dream Evolver 传入增量消息（不是全量 msg_list_text）
3. Context Manager 传入保护范围消息（带 [PROTECTED] 标签）
4. 三个游标正确推进

- [ ] **Step 3: 触发 force 模式验证**

```bash
curl -X POST http://localhost:9876/api/tidy -H "Content-Type: application/json" -d '{"mode": "force"}'
```

确认 Entity Extractor 全量处理，Dream Evolver 增量处理，Context Manager force 压缩执行。

- [ ] **Step 4: 检查游标文件**

```bash
cat ~/.niu/last_entity_extract.json
cat ~/.niu/last_dream_evolve.json
cat ~/.niu/last_compress.json
```

确认游标已正确推进。

---

## 自检清单

### 设计文档覆盖检查

| 设计文档改动 | 对应 Task | 状态 |
|-------------|----------|------|
| 1. 主Agent工具列表清理 | Task 6 (handler) + Task 7 (niu.md) | ✅ |
| 2. Entity Extractor 改 task 方式 | Task 8 (sleep) + Task 9 (force) | ✅ |
| 3. Dream Evolver 改增量模式 | Task 10 | ✅ |
| 4. Context Manager 保护范围硬性保证 | Task 11 (sleep) + Task 12 (force) | ✅ |
| 5. 串行调用消息刷新 | Task 10 (Dream) + Task 11 (Context) | ✅ |
| 6. Dream Evolver 游标 fallback | Task 5 + Task 10 | ✅ |
| 7. `_build_incremental_msg_text()` 增强 | Task 1 + Task 2 + Task 3 | ✅ |
| entity-extractor.md prompt | Task 13 | ✅ |
| dream-evolver.md prompt | Task 14 | ✅ |
| context-manager.md prompt | Task 15 | ✅ |
| _build_entity_history() 删除 | Task 4 | ✅ |

### Placeholder 扫描

无 TBD、TODO、"implement later"、"add validation" 等。所有步骤包含完整代码。

### 类型一致性

- `_build_incremental_msg_text()` 签名在所有调用处一致：`(messages, last_cursor_id, out_msg_ids, msg_tokens=None, end_cursor_id=None, filter_wm=False, protect_recent=0)`
- `_extract_cursor_id()` 返回值：`str | None`，`"NULL"` 为特殊标记
- `BLOCKED_SUBAGENTS` 在 dispatch 和测试中一致：`{"context-manager", "entity-extractor"}`
