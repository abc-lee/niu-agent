# 子 Agent FIFO 保底 + dream-evolver 拆分 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为所有子 Agent 开启 FIFO 保底防止溢出死循环，并在 dream-evolver 增量消息 token 量过大时拆分成两批调用。

**Architecture:** Part 1 将 9 处 `context_fifo_threshold=0` 改为 `-1`（开启 FIFO fallback truncation）。Part 2 新增 `_split_dream_first_batch` 函数在 user 消息边界处拆分 dream-evolver 的增量消息，只返回第一批 ID（`list[str] | None`）；第二批的范围由大模型第一批输出的 `processed_up_to` 对应游标之后动态计算，将 dream-evolver 的 sleep/force 路径从单次调用改为显式两批调用。

**Tech Stack:** Python 3.11+, pytest, niu_api/compat.py, agent/generic/agent_loop.py

---

## 文件结构

| 文件 | 责任 | 操作 |
|------|------|------|
| `niu_api/compat.py` | 主 tidy 管线，子 Agent 调用编排 | 修改 |
| `agent/runner.py` | 主 Agent 上下文超阈值回调中的 force 压缩路径 | 修改 |
| `tests/test_dream_split.py` | 测试 `_split_dream_first_batch` 拆分算法 | 新建 |

---

### Task 1: 新增 `_split_dream_first_batch` 函数

**Files:**
- Modify: `niu_api/compat.py` (在 `_find_protected_range` 函数之后，约 L358 位置插入)
- Test: `tests/test_dream_split.py`

- [ ] **Step 1: 写失败测试 — 不拆分场景**

```python
"""Tests for _split_dream_first_batch — dream-evolver first-batch splitting at user boundaries."""

from niu_api.compat import _split_dream_first_batch


class _Msg:
    """Lightweight message object with .role, .id, .content attributes."""
    def __init__(self, role, mid, content="x"):
        self.role = role
        self.id = mid
        self.content = content


def _msgs(*roles):
    """Build a list of _Msg objects from role strings. IDs are id-0, id-1, etc."""
    return [_Msg(r, f"id-{i}") for i, r in enumerate(roles)]


def _ids(msgs):
    """Extract IDs from a list of _Msg objects."""
    return [m.id for m in msgs]


def _tokens_for(msgs, per_msg_tokens=10):
    """Build a msg_tokens list matching msgs, each with per_msg_tokens."""
    return [per_msg_tokens] * len(msgs)


class TestSplitDreamFirstBatchNoSplit:
    """Tests for scenarios where splitting should NOT occur."""

    def test_below_threshold_no_split(self):
        """Incremental tokens < 50% of context window → no split (None)."""
        msgs = _msgs("user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=10)  # 40 tokens total
        context_window = 1000  # 40/1000 = 4% << 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None

    def test_too_few_messages_no_split(self):
        """Fewer than 4 messages → no split even if tokens are high."""
        msgs = _msgs("user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=800)  # 1600 tokens total
        context_window = 1000  # 150% >> 50% but only 2 messages
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python/bin/python -m pytest tests/test_dream_split.py::TestSplitDreamFirstBatchNoSplit -v`
Expected: FAIL with `ImportError: cannot import name '_split_dream_first_batch'`

- [ ] **Step 3: 实现 `_split_dream_first_batch` 函数**

在 `niu_api/compat.py` 中 `_find_protected_range` 函数之后（约 L358，`_build_incremental_msg_text` 之前）插入：

```python
def _split_dream_first_batch(
    messages: list,
    dream_msg_ids: list[str],
    msg_tokens: list[int],
    context_window_tokens: int,
    threshold: float = 0.50,
) -> list[str] | None:
    """计算 dream-evolver 第一批的消息 ID 列表。

    当增量消息的 token 总量 >= 上下文窗口的 threshold 时，
    在中间位置向两端查找最近的 role=user 消息作为分割点，
    返回第一批的消息 ID 列表（split_pos 之前，不含 user 消息）。
    无需拆分时返回 None。

    Args:
        messages: 全量消息列表（Message 对象，含 id/role/content）
        dream_msg_ids: 增量消息 UUID 列表（按顺序）
        msg_tokens: 每条消息的 token 数（与 messages 等长同顺序）
        context_window_tokens: 子 Agent 上下文窗口大小
        threshold: 拆分阈值（默认 0.50 = 50%）

    Returns:
        第一批消息 ID 列表，或 None（无需拆分）。
    """
    if len(dream_msg_ids) < 4 or context_window_tokens <= 0:
        return None

    # 计算增量消息 token 总量
    _id_set = set(dream_msg_ids)
    incremental_tokens = 0
    for i, msg in enumerate(messages):
        if (getattr(msg, "id", "") or "") in _id_set and i < len(msg_tokens):
            incremental_tokens += msg_tokens[i]

    if incremental_tokens < context_window_tokens * threshold:
        return None

    # 构建增量消息子列表（保持原序）
    dream_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]

    mid = len(dream_incremental_msgs) // 2

    # 从 mid 向两端查找最近的 role=user 消息
    right_user = None
    for i in range(mid, len(dream_incremental_msgs)):
        if getattr(dream_incremental_msgs[i], "role", "") == "user":
            right_user = i
            break

    left_user = None
    for i in range(mid - 1, -1, -1):
        if getattr(dream_incremental_msgs[i], "role", "") == "user":
            left_user = i
            break

    # 确定分割点
    if left_user is not None and right_user is not None:
        if (mid - left_user) <= (right_user - mid):
            split_pos = left_user
        else:
            split_pos = right_user
    elif left_user is not None:
        split_pos = left_user
    elif right_user is not None:
        split_pos = right_user
    else:
        return None

    first_batch = dream_msg_ids[:split_pos]

    if not first_batch:
        return None

    return first_batch
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python/bin/python -m pytest tests/test_dream_split.py::TestSplitDreamFirstBatchNoSplit -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 写失败测试 — 拆分场景**

在 `tests/test_dream_split.py` 中追加：

```python
class TestSplitDreamFirstBatchSplit:
    """Tests for scenarios where splitting SHOULD occur."""

    def test_split_at_nearest_user(self):
        """8 messages, tokens exceed threshold, split at nearest user to midpoint.

        Messages: [user, assistant, user, assistant, user, assistant, user, assistant]
        mid = 4 (0-indexed), msg[4] = user → split_pos = 4
        first_batch = ids[0:4]
        """
        msgs = _msgs("user", "assistant", "user", "assistant",
                      "user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=100)  # 800 tokens total
        context_window = 1000  # 80% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        assert result == dream_ids[:4]

    def test_split_picks_closer_user_right(self):
        """When right user is closer to mid, pick right.

        Messages: [user, assistant, assistant, user, assistant, assistant]
        mid = 3, msg[3] = user (right_user). left_user = 0 (user at index 0).
        dist left = 3-0 = 3, dist right = 3-3 = 0 → right closer → split_pos = 3.
        """
        msgs = _msgs("user", "assistant", "assistant", "user", "assistant", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=200)  # 1200 tokens
        context_window = 1000  # 120% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        assert result == dream_ids[:3]

    def test_split_no_user_messages_no_split(self):
        """All tool/assistant messages, no user → no split even if tokens high."""
        msgs = _msgs("assistant", "tool", "assistant", "tool",
                      "assistant", "tool", "assistant", "tool")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=200)  # 1600 tokens
        context_window = 1000  # 150% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None

    def test_split_first_batch_excludes_user_at_split_pos(self):
        """Verify first_batch excludes the user message at the split point.

        Messages: [assistant, assistant, user, assistant, assistant, assistant, user, assistant]
        mid = 4, msg[4] = assistant. right_user = 6 (user). left_user = 2 (user).
        dist left = 4-2 = 2, dist right = 6-4 = 2 → equidistant → pick left → split_pos = 2.
        first_batch = ids[0:2] (excludes the user at index 2).
        """
        msgs = _msgs("assistant", "assistant", "user", "assistant",
                      "assistant", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=100)  # 800 tokens
        context_window = 1000  # 80% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        # split_pos=2, first_batch = dream_ids[:2], excludes user at index 2
        assert result == dream_ids[:2]
        # The user at index 2 is NOT in first_batch
        assert "id-2" not in result

    def test_threshold_just_below_no_split(self):
        """Tokens at 48.8% → no split (below 50% threshold).

        incremental_tokens (488) < context_window * 0.50 (500) → True → no split.
        """
        msgs = _msgs("user", "assistant", "user", "assistant",
                      "user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=61)  # 488 tokens
        context_window = 1000  # 48.8% < 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None

    def test_threshold_just_above_splits(self):
        """Tokens at 50.4% → split (at or above 50% threshold).

        incremental_tokens (504) < context_window * 0.50 (500) → False → split.
        """
        msgs = _msgs("user", "assistant", "user", "assistant",
                      "user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=63)  # 504 tokens
        context_window = 1000  # 50.4% >= 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        assert result == dream_ids[:4]
```

- [ ] **Step 6: 运行全部测试确认通过**

Run: `python/bin/python -m pytest tests/test_dream_split.py -v`
Expected: PASS (all tests)

- [ ] **Step 7: 提交**

```bash
git add niu_api/compat.py tests/test_dream_split.py
git commit -m "feat: add _split_dream_first_batch function for dream-evolver first-batch splitting"
```

---

### Task 2: 全部子 Agent 开启 FIFO（`context_fifo_threshold=0` → `-1`）

**Files:**
- Modify: `niu_api/compat.py` (9 处)
- Modify: `agent/runner.py` (4 处)

这是纯机械替换，共 13 处 `context_fifo_threshold=0` 改为 `context_fifo_threshold=-1`。

**compat.py 9 处**：

1. L2418: `context_fifo_threshold=0,  # 关闭 FIFO，保留完整上下文` → `context_fifo_threshold=-1,  # FIFO 保底：首轮裁剪到 75%，防止溢出死循环`
2. L2499: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
3. L2581: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
4. L2752: `context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文` → `context_fifo_threshold=-1,  # FIFO 保底`
5. L2971: `context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文` → `context_fifo_threshold=-1,  # FIFO 保底`
6. L3146: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
7. L3225: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
8. L3307: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
9. L3419: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`

注意：有些行有注释 `# 关闭 FIFO，保留完整上下文` 或 `# 关闭FIFO，保留完整上下文`，需要一起替换。

**runner.py 4 处**（R3 审查发现，_on_context_high_usage 回调中的 force 压缩路径）：

10. L1238: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
11. L1277: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
12. L1313: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`
13. L1384: `context_fifo_threshold=0,` → `context_fifo_threshold=-1,  # FIFO 保底`

- [ ] **Step 2: 验证替换数量**

Run: `grep -c "context_fifo_threshold=0" niu_api/compat.py`
Expected: 0

Run: `grep -c "context_fifo_threshold=-1" niu_api/compat.py`
Expected: 9

Run: `grep -c "context_fifo_threshold=0" agent/runner.py`
Expected: 0

Run: `grep -c "context_fifo_threshold=-1" agent/runner.py`
Expected: 4

- [ ] **Step 3: 运行已有测试确认无回归**

Run: `python/bin/python -m pytest tests/test_protect_range.py tests/test_sep_cleanup.py tests/test_dream_split.py -v`
Expected: PASS (all tests)

- [ ] **Step 4: 提交**

```bash
git add niu_api/compat.py agent/runner.py
git commit -m "feat: enable FIFO fallback for all sub-agents (context_fifo_threshold 0→-1)

Prevents overflow death-loop: if history exceeds context window on
first turn, fallback truncation prunes to 75% before first LLM call.
Proactive pruning (80% threshold) continues to protect subsequent turns.
Covers both compat.py (9 sites) and runner.py (4 sites)."
```

---

### Task 3: dream-evolver sleep 路径增加拆分逻辑

**Files:**
- Modify: `niu_api/compat.py:2477-2538` (dream-evolver sleep 块)

将 dream-evolver sleep 路径从"单次调用"改为"显式两批调用"。程序只负责"砍一半"生成第一批（`_split_dream_first_batch` 返回 `list[str] | None`），第二批的范围由大模型第一批输出的 `processed_up_to` 对应游标之后动态计算。

- [ ] **Step 1: 读取当前代码确认行号**

Run: `grep -n "dream-evolver" niu_api/compat.py | head -20`
确认 sleep 路径 dream-evolver 块从 L2477 开始。

- [ ] **Step 2: 替换 dream-evolver sleep 块**

将 L2482-2538（`if dream_msg_ids:` 块的内部逻辑）替换为显式两批调用的逻辑。

**替换前**（L2482-2538 核心结构）：
```python
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_task_prompt = """...(略)..."""
                # 构造增量 history
                _id_set = set(dream_msg_ids)
                dream_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                dream_history, dream_idx_to_id = _build_plain_history(dream_incremental_msgs)

                def run_dream_evolver():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=dream_task_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=dream_history,
                        context_fifo_threshold=-1,  # Task 2 已改
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

                # 游标推进
                if _is_subagent_overflow(dream_result):
                    ...
                else:
                    ...推进游标...
                # 校验游标 + 写入游标
                ...
```

**替换后**：
```python
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_task_prompt = """对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

                # 计算第一批：增量消息 token 量过大时在 user 消息边界处拆分
                _dream_context_window = _read_context_window_tokens()
                _first_batch_ids = _split_dream_first_batch(
                    messages, dream_msg_ids, msg_tokens, _dream_context_window
                )

                if _first_batch_ids is None:
                    # 不拆分，正常单次调用（保持原有逻辑）
                    _id_set = set(dream_msg_ids)
                    dream_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                    dream_history, dream_idx_to_id = _build_plain_history(dream_incremental_msgs)

                    def run_dream_evolver():
                        return call_subagent_with_auto_answer(
                            agent_name="dream-evolver",
                            task=dream_task_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            history=dream_history,
                            context_fifo_threshold=-1,
                        )

                    dream_result = await asyncio.to_thread(run_dream_evolver)
                    if is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

                    # 游标推进：overflow→不动；否则解析 processed_up_to=N
                    if _is_subagent_overflow(dream_result):
                        overflow_info = _extract_overflow_info(dream_result)
                        logger.warning(f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    else:
                        _processed_idx = _parse_processed_up_to(dream_result)
                        if _processed_idx is not None and _processed_idx in dream_idx_to_id:
                            new_dream_id = dream_idx_to_id[_processed_idx]
                            logger.info(f"[Tidy] Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                        elif dream_msg_ids:
                            new_dream_id = dream_msg_ids[-1]  # 兜底
                            logger.info(f"[Tidy] Dream cursor fallback to range end: {new_dream_id}")
                        else:
                            new_dream_id = last_dream_evolve_id
                    # 校验游标 + 写入游标
                    if new_dream_id:
                        fresh_msgs = await store.get_messages()
                        fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        if new_dream_id not in fresh_ids:
                            logger.warning(f"[Tidy] Dream cursor {new_dream_id} deleted by sub-agent, reverting to {last_dream_evolve_id}")
                            new_dream_id = last_dream_evolve_id
                            if new_dream_id and new_dream_id not in fresh_ids:
                                new_dream_id = ""
                    if new_dream_id:
                        _write_cursor_with_lock(dream_cursor_path, {
                            "last_dream_evolve_id": new_dream_id,
                            "last_evolve_at": datetime.now().isoformat(),
                        })
                        logger.info(f"[Tidy] Dream cursor updated: last_dream_evolve_id={new_dream_id}")
                        last_dream_evolve_id = new_dream_id
                else:
                    # ===== 第一批 =====
                    logger.info(f"[Tidy] dream-evolver: splitting into 2 batches "
                                f"(incremental tokens exceed 50% of {_dream_context_window})")
                    _first_id_set = set(_first_batch_ids)
                    _first_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _first_id_set]
                    _first_history, _first_idx_to_id = _build_plain_history(_first_msgs)

                    def _run_dream_evolver_batch1():
                        return call_subagent_with_auto_answer(
                            agent_name="dream-evolver",
                            task=dream_task_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            history=_first_history,
                            context_fifo_threshold=-1,
                        )

                    logger.info(f"[Tidy] dream-evolver batch 1/2: {len(_first_batch_ids)} messages")
                    dream_result = await asyncio.to_thread(_run_dream_evolver_batch1)
                    if is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] dream-evolver batch 1/2 result: {dream_result[:200]}")

                    # 游标推进：overflow→不动并跳过第二批；否则解析 processed_up_to=N
                    if _is_subagent_overflow(dream_result):
                        overflow_info = _extract_overflow_info(dream_result)
                        logger.warning(f"[Tidy] dream-evolver batch 1/2 overflow: "
                                       f"{overflow_info.get('turns_completed', 0)} turns, "
                                       f"{overflow_info.get('tokens_used', 0)} tokens")
                        # overflow 时游标不动，跳过第二批
                    else:
                        _processed_idx = _parse_processed_up_to(dream_result)
                        if _processed_idx is not None and _processed_idx in _first_idx_to_id:
                            new_dream_id = _first_idx_to_id[_processed_idx]
                            logger.info(f"[Tidy] Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                        elif _first_batch_ids:
                            new_dream_id = _first_batch_ids[-1]  # 兜底
                            logger.info(f"[Tidy] Dream cursor fallback to batch end: {new_dream_id}")
                        else:
                            new_dream_id = last_dream_evolve_id
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
                        _write_cursor_with_lock(dream_cursor_path, {
                            "last_dream_evolve_id": new_dream_id,
                            "last_evolve_at": datetime.now().isoformat(),
                        })
                        logger.info(f"[Tidy] Dream cursor updated: last_dream_evolve_id={new_dream_id}")
                        last_dream_evolve_id = new_dream_id  # 更新基准，使第二批回退到此游标而非循环前旧值

                        # ===== 第二批：从 new_dream_id 之后到末尾，动态计算 =====
                        _second_batch_ids = []
                        _found_cursor = False
                        for mid in dream_msg_ids:
                            if mid == new_dream_id:
                                _found_cursor = True
                                continue
                            if _found_cursor:
                                _second_batch_ids.append(mid)

                        if _second_batch_ids:
                            _second_id_set = set(_second_batch_ids)
                            _second_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _second_id_set]
                            _second_history, _second_idx_to_id = _build_plain_history(_second_msgs)

                            def _run_dream_evolver_batch2():
                                return call_subagent_with_auto_answer(
                                    agent_name="dream-evolver",
                                    task=dream_task_prompt,
                                    llm_config=llm_config,
                                    mcp_client=None,
                                    history=_second_history,
                                    context_fifo_threshold=-1,
                                )

                            logger.info(f"[Tidy] dream-evolver batch 2/2: {len(_second_batch_ids)} messages")
                            dream_result = await asyncio.to_thread(_run_dream_evolver_batch2)
                            if is_stop_requested():
                                logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                                clear_stop()
                                return {"status": "aborted", "message": "Stopped by user"}
                            logger.info(f"[Tidy] dream-evolver batch 2/2 result: {dream_result[:200]}")

                            # 游标推进：overflow→不动；否则解析 processed_up_to=N
                            if _is_subagent_overflow(dream_result):
                                overflow_info = _extract_overflow_info(dream_result)
                                logger.warning(f"[Tidy] dream-evolver batch 2/2 overflow: "
                                               f"{overflow_info.get('turns_completed', 0)} turns, "
                                               f"{overflow_info.get('tokens_used', 0)} tokens")
                            else:
                                _processed_idx = _parse_processed_up_to(dream_result)
                                if _processed_idx is not None and _processed_idx in _second_idx_to_id:
                                    new_dream_id = _second_idx_to_id[_processed_idx]
                                    logger.info(f"[Tidy] Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                                elif _second_batch_ids:
                                    new_dream_id = _second_batch_ids[-1]  # 兜底
                                    logger.info(f"[Tidy] Dream cursor fallback to batch end: {new_dream_id}")
                                else:
                                    new_dream_id = last_dream_evolve_id
                            # 校验游标 + 写入游标
                            if new_dream_id:
                                fresh_msgs = await store.get_messages()
                                fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                                if new_dream_id not in fresh_ids:
                                    logger.warning(f"[Tidy] Dream cursor {new_dream_id} deleted by sub-agent, reverting to {last_dream_evolve_id}")
                                    new_dream_id = last_dream_evolve_id
                                    if new_dream_id and new_dream_id not in fresh_ids:
                                        new_dream_id = ""
                            if new_dream_id:
                                _write_cursor_with_lock(dream_cursor_path, {
                                    "last_dream_evolve_id": new_dream_id,
                                    "last_evolve_at": datetime.now().isoformat(),
                                })
                                logger.info(f"[Tidy] Dream cursor updated: last_dream_evolve_id={new_dream_id}")
                                last_dream_evolve_id = new_dream_id
            else:
                logger.info("[Tidy] dream-evolver: no new messages since cursor")
                new_dream_id = last_dream_evolve_id
```

**关键变更点**：
1. 新增 `_split_dream_first_batch` 调用，只计算第一批（返回 `list[str] | None`）
2. `_first_batch_ids is None` 时走原有单次调用逻辑（不拆分）
3. `_first_batch_ids is not None` 时走显式两批调用：
   - 第一批：用 `_first_batch_ids` 构造 history，调用子 Agent，解析 `processed_up_to` 得到 `new_dream_id`，写游标，`last_dream_evolve_id = new_dream_id`
   - 第二批：在 `dream_msg_ids` 中找到 `new_dream_id` 的索引，取其后所有 ID 作为 `_second_batch_ids`（动态计算，不是预计算的）
4. overflow 时跳过第二批（游标不动），第一批 overflow 后不进入第二批逻辑
5. 第二批的范围由第一批的 `new_dream_id` 决定：如果大模型在第一批只处理了 90/110 条，第二批从第 91 条开始（包含第一批未处理部分 + 原计划后半段）
6. `last_dream_evolve_id = new_dream_id` 在第一批后更新，使第二批的回退基准正确

- [ ] **Step 3: 运行已有测试确认无回归**

Run: `python/bin/python -m pytest tests/test_protect_range.py tests/test_sep_cleanup.py tests/test_dream_split.py -v`
Expected: PASS (all tests)

- [ ] **Step 4: 提交**

```bash
git add niu_api/compat.py
git commit -m "feat: dream-evolver sleep path — split large incremental range into batches

When incremental message tokens exceed 50% of context window, split at
nearest user message boundary into two batches. Each batch gets its own
sub-agent call with independent cursor advancement. Overflow on first
batch skips remaining batches."
```

---

### Task 4: dream-evolver force 路径增加拆分逻辑

**Files:**
- Modify: `niu_api/compat.py:3203-3252` (dream-evolver force 块)

与 Task 3 相同的显式两批逻辑，但作用于 force 路径。差异：
- `new_dream_id` 在 `if dream_force_msg_ids:` 块开头初始化为 `last_dream_evolve_id`（R1 P0 修复）
- 游标校验在循环外（force 路径既有行为，L3254-3262 保持不变）
- 第二批的 `_second_batch_ids` 用 `dream_force_msg_ids` 而非 `dream_msg_ids`

- [ ] **Step 1: 读取当前代码确认行号**

确认 force 路径 dream-evolver 块从 L3203 开始。

- [ ] **Step 2: 替换 dream-evolver force 块**

将 L3209-3249（`if dream_force_msg_ids:` 块的内部逻辑）替换为显式两批调用的逻辑。

**替换后**：
```python
            if dream_force_msg_ids:
                new_dream_id = last_dream_evolve_id  # 初始化，防止 overflow break 时未定义
                dream_force_prompt = """对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

                # 计算第一批：增量消息 token 量过大时在 user 消息边界处拆分
                _dream_context_window = _read_context_window_tokens()
                _first_batch_ids = _split_dream_first_batch(
                    messages, dream_force_msg_ids, msg_tokens, _dream_context_window
                )

                if _first_batch_ids is None:
                    # 不拆分，正常单次调用（保持原有逻辑）
                    _id_set = set(dream_force_msg_ids)
                    dream_force_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                    dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                    def run_dream_evolver_force():
                        return call_subagent_with_auto_answer(
                            agent_name="dream-evolver",
                            task=dream_force_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            history=dream_force_history,
                            context_fifo_threshold=-1,
                        )

                    dream_result = await asyncio.to_thread(run_dream_evolver_force)
                    if is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] Force: dream-evolver completed, length={len(dream_result)}")

                    # 游标推进：overflow→不动；否则解析 processed_up_to=N
                    if _is_subagent_overflow(dream_result):
                        overflow_info = _extract_overflow_info(dream_result)
                        logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    else:
                        _processed_idx = _parse_processed_up_to(dream_result)
                        if _processed_idx is not None and _processed_idx in dream_force_idx_to_id:
                            new_dream_id = dream_force_idx_to_id[_processed_idx]
                            logger.info(f"[Tidy] Force: Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                        elif dream_force_msg_ids:
                            new_dream_id = dream_force_msg_ids[-1]  # 兜底
                            logger.info(f"[Tidy] Force: Dream cursor fallback to range end: {new_dream_id}")
                        else:
                            new_dream_id = last_dream_evolve_id
                else:
                    # ===== 第一批 =====
                    logger.info(f"[Tidy] Force: dream-evolver splitting into 2 batches "
                                f"(incremental tokens exceed 50% of {_dream_context_window})")
                    _first_id_set = set(_first_batch_ids)
                    _first_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _first_id_set]
                    _first_history, _first_idx_to_id = _build_plain_history(_first_msgs)

                    def _run_dream_evolver_force_batch1():
                        return call_subagent_with_auto_answer(
                            agent_name="dream-evolver",
                            task=dream_force_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            history=_first_history,
                            context_fifo_threshold=-1,
                        )

                    logger.info(f"[Tidy] Force: dream-evolver batch 1/2: {len(_first_batch_ids)} messages")
                    dream_result = await asyncio.to_thread(_run_dream_evolver_force_batch1)
                    if is_stop_requested():
                        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                        clear_stop()
                        return {"status": "aborted", "message": "Stopped by user"}
                    logger.info(f"[Tidy] Force: dream-evolver batch 1/2 result: {dream_result[:200]}")

                    # 游标推进：overflow→不动并跳过第二批；否则解析 processed_up_to=N
                    if _is_subagent_overflow(dream_result):
                        overflow_info = _extract_overflow_info(dream_result)
                        logger.warning(f"[Tidy] Force: dream-evolver batch 1/2 overflow: "
                                       f"{overflow_info.get('turns_completed', 0)} turns, "
                                       f"{overflow_info.get('tokens_used', 0)} tokens")
                        # overflow 时游标不动，跳过第二批
                    else:
                        _processed_idx = _parse_processed_up_to(dream_result)
                        if _processed_idx is not None and _processed_idx in _first_idx_to_id:
                            new_dream_id = _first_idx_to_id[_processed_idx]
                            logger.info(f"[Tidy] Force: Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                        elif _first_batch_ids:
                            new_dream_id = _first_batch_ids[-1]  # 兜底
                            logger.info(f"[Tidy] Force: Dream cursor fallback to batch end: {new_dream_id}")
                        else:
                            new_dream_id = last_dream_evolve_id
                        last_dream_evolve_id = new_dream_id  # 更新基准，使第二批回退到此游标

                        # ===== 第二批：从 new_dream_id 之后到末尾，动态计算 =====
                        _second_batch_ids = []
                        _found_cursor = False
                        for mid in dream_force_msg_ids:
                            if mid == new_dream_id:
                                _found_cursor = True
                                continue
                            if _found_cursor:
                                _second_batch_ids.append(mid)

                        if _second_batch_ids:
                            _second_id_set = set(_second_batch_ids)
                            _second_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _second_id_set]
                            _second_history, _second_idx_to_id = _build_plain_history(_second_msgs)

                            def _run_dream_evolver_force_batch2():
                                return call_subagent_with_auto_answer(
                                    agent_name="dream-evolver",
                                    task=dream_force_prompt,
                                    llm_config=llm_config,
                                    mcp_client=None,
                                    history=_second_history,
                                    context_fifo_threshold=-1,
                                )

                            logger.info(f"[Tidy] Force: dream-evolver batch 2/2: {len(_second_batch_ids)} messages")
                            dream_result = await asyncio.to_thread(_run_dream_evolver_force_batch2)
                            if is_stop_requested():
                                logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                                clear_stop()
                                return {"status": "aborted", "message": "Stopped by user"}
                            logger.info(f"[Tidy] Force: dream-evolver batch 2/2 result: {dream_result[:200]}")

                            # 游标推进：overflow→不动；否则解析 processed_up_to=N
                            if _is_subagent_overflow(dream_result):
                                overflow_info = _extract_overflow_info(dream_result)
                                logger.warning(f"[Tidy] Force: dream-evolver batch 2/2 overflow: "
                                               f"{overflow_info.get('turns_completed', 0)} turns, "
                                               f"{overflow_info.get('tokens_used', 0)} tokens")
                            else:
                                _processed_idx = _parse_processed_up_to(dream_result)
                                if _processed_idx is not None and _processed_idx in _second_idx_to_id:
                                    new_dream_id = _second_idx_to_id[_processed_idx]
                                    logger.info(f"[Tidy] Force: Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                                elif _second_batch_ids:
                                    new_dream_id = _second_batch_ids[-1]  # 兜底
                                    logger.info(f"[Tidy] Force: Dream cursor fallback to batch end: {new_dream_id}")
                                else:
                                    new_dream_id = last_dream_evolve_id
            else:
                logger.info("[Tidy] Force: dream-evolver no incremental messages")
                new_dream_id = last_dream_evolve_id  # 无增量时保留旧游标，避免 UnboundLocalError
```

**注意**：force 路径的游标校验在 L3254-3262（循环外），保持不变。只替换 `if dream_force_msg_ids:` 块内部逻辑。force 路径不在批次内写游标，只在 `if/else` 块结束后由 L3254-3262 统一校验和写入。

**重要差异**：force 路径的游标校验（`fresh_msgs` 检查 + `new_dream_id` 重置）在 L3254-3262 是在 `if/else` 块之外执行的。因此 force 路径的游标校验和写入**不在批次内执行**——每批结束后只更新 `new_dream_id` 变量，`if/else` 块结束后统一校验和写入。

`new_dream_id` 在 `if dream_force_msg_ids:` 块开头初始化为 `last_dream_evolve_id`（R1 P0 修复）。各种场景：

- 不拆分 + overflow → `new_dream_id` 保持初始值 `last_dream_evolve_id` → 循环外校验通过
- 不拆分 + 正常 → `new_dream_id` = processed_up_to 对应 ID → 正确
- 第一批 overflow → 跳过第二批，`new_dream_id` 保持初始值 `last_dream_evolve_id` → 循环外校验通过
- 第一批成功、第二批 overflow → `new_dream_id` 保持第一批的值 → 循环外校验通过
- 两批都正常完成 → `new_dream_id` 是第二批的值 → 正确

所以 force 路径不需要在批次内写游标，只需在 `if/else` 块结束后统一校验和写入。

- [ ] **Step 3: 运行已有测试确认无回归**

Run: `python/bin/python -m pytest tests/test_protect_range.py tests/test_sep_cleanup.py tests/test_dream_split.py -v`
Expected: PASS (all tests)

- [ ] **Step 4: 提交**

```bash
git add niu_api/compat.py
git commit -m "feat: dream-evolver force path — split large incremental range into batches

Same splitting logic as sleep path: when incremental tokens exceed 50%
of context window, split at user message boundary. Overflow on first
batch breaks out of loop, cursor stays at last successful position."
```

### Task 4b: runner.py dream-evolver force 路径增加拆分逻辑

**Files:**
- Modify: `agent/runner.py:1264-1285` (dream-evolver force 块)

R3 审查发现 `agent/runner.py` 的 `_on_context_high_usage` 回调中有另一条 force 压缩路径，其中 dream-evolver 调用（L1264-1285）没有拆分逻辑。

runner.py 的 dream-evolver force 路径使用 `_run_subagent_step` 封装（而非 compat.py 直接调用 `call_subagent_with_auto_answer`）。`_run_subagent_step` 内部处理了游标推进、校验、写入，支持多次调用。

- [ ] **Step 1: 读取当前代码确认行号**

确认 runner.py dream-evolver force 块从 L1264 开始。

- [ ] **Step 2: 添加导入**

在 `agent/runner.py` 的 `_on_context_high_usage` 方法导入块（L1134-1143）中添加 `_is_subagent_overflow` 和 `_split_dream_first_batch`：

```python
        from niu_api.compat import (
            _build_compress_history,
            _build_force_prompt,
            _build_incremental_msg_text,
            _build_journal_task,
            _build_plain_history,
            _is_subagent_overflow,      # 新增：检测 overflow
            _parse_idx_list,
            _split_dream_first_batch,   # 新增：dream-evolver 第一批拆分
            _strip_analysis,
            _write_cursor_with_lock,
        )
```

注意：`_is_subagent_overflow` 在 `_run_subagent_step` 方法内部也有局部导入（L1058），但那是方法级作用域，在 `_on_context_high_usage` 中不可用。必须在此处导入块中添加。

- [ ] **Step 3: 替换 dream-evolver force 块**

将 L1264-1285（`if dream_force_msg_ids:` 块）替换为显式两批调用的逻辑。

```python
            if dream_force_msg_ids:
                dream_force_prompt = """对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

                # 计算第一批：增量消息 token 量过大时在 user 消息边界处拆分
                _dream_context_window = _read_context_window_tokens()
                _first_batch_ids = _split_dream_first_batch(
                    db_messages, dream_force_msg_ids, msg_tokens, _dream_context_window
                )

                if _first_batch_ids is None:
                    # 不拆分，正常单次调用（保持原有逻辑）
                    _id_set = set(dream_force_msg_ids)
                    dream_force_incremental_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                    dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                    _, new_dream_id = self._run_subagent_step(
                        "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                        dream_force_prompt, llm_config, last_dream_evolve_id,
                        dream_force_msg_ids, "last_evolve_at",
                        history=dream_force_history, context_fifo_threshold=-1,
                        idx_to_id=dream_force_idx_to_id,
                    )

                    if is_stop_requested():
                        logger.warning("[Runner] Stop requested, aborting force compress")
                        return
                else:
                    # ===== 第一批 =====
                    logger.info(f"[Runner] Force: dream-evolver splitting into 2 batches "
                                f"(incremental tokens exceed 50% of {_dream_context_window})")
                    _first_id_set = set(_first_batch_ids)
                    _first_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _first_id_set]
                    _first_history, _first_idx_to_id = _build_plain_history(_first_msgs)

                    logger.info(f"[Runner] Force: dream-evolver batch 1/2: {len(_first_batch_ids)} messages")
                    _batch_result, new_dream_id = self._run_subagent_step(
                        "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                        dream_force_prompt, llm_config, last_dream_evolve_id,
                        _first_batch_ids, "last_evolve_at",
                        history=_first_history, context_fifo_threshold=-1,
                        idx_to_id=_first_idx_to_id,
                    )

                    if is_stop_requested():
                        logger.warning("[Runner] Stop requested, aborting force compress")
                        return

                    # overflow 检测：跳过第二批（游标不动，_run_subagent_step 已处理）
                    if not _is_subagent_overflow(_batch_result):
                        last_dream_evolve_id = new_dream_id  # 更新基准，使第二批回退到此游标

                        # ===== 第二批：从 new_dream_id 之后到末尾，动态计算 =====
                        _second_batch_ids = []
                        _found_cursor = False
                        for mid in dream_force_msg_ids:
                            if mid == new_dream_id:
                                _found_cursor = True
                                continue
                            if _found_cursor:
                                _second_batch_ids.append(mid)

                        if _second_batch_ids:
                            _second_id_set = set(_second_batch_ids)
                            _second_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _second_id_set]
                            _second_history, _second_idx_to_id = _build_plain_history(_second_msgs)

                            logger.info(f"[Runner] Force: dream-evolver batch 2/2: {len(_second_batch_ids)} messages")
                            _batch_result, new_dream_id = self._run_subagent_step(
                                "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                                dream_force_prompt, llm_config, last_dream_evolve_id,
                                _second_batch_ids, "last_evolve_at",
                                history=_second_history, context_fifo_threshold=-1,
                                idx_to_id=_second_idx_to_id,
                            )

                            if is_stop_requested():
                                logger.warning("[Runner] Stop requested, aborting force compress")
                                return
            else:
                logger.info("[Runner] Force: dream-evolver no incremental messages")
```

**关键说明**：
- `_run_subagent_step` 返回 `(result, new_cursor_id)`。`result` 可用 `_is_subagent_overflow` 检测 overflow。
- overflow 时 `_run_subagent_step` 内部已处理游标不动（返回 `last_cursor_id`），第一批 overflow 时跳过第二批（不进入第二批逻辑）。
- `last_dream_evolve_id = new_dream_id` 在第一批成功后更新，使第二批的 `_run_subagent_step` 调用使用第一批的游标作为 `last_cursor_id` 参数。
- 第二批的范围由第一批的 `new_dream_id` 动态决定：在 `dream_force_msg_ids` 中找到 `new_dream_id` 的索引，取其后所有 ID。
- 不拆分时（`_first_batch_ids is None`）走原有单次调用逻辑。

- [ ] **Step 4: 运行已有测试确认无回归**

Run: `python/bin/python -m pytest tests/test_protect_range.py tests/test_sep_cleanup.py tests/test_dream_split.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: 提交**

```bash
git add agent/runner.py
git commit -m "feat: runner.py dream-evolver force path — split large incremental range into batches

R3 review found runner.py _on_context_high_usage has a parallel force compress
path missing dream-evolver split logic. Now uses _split_dream_first_batch same
as compat.py. Program computes first batch; second batch range is determined
dynamically from the first batch's processed_up_to cursor. Overflow on first
batch skips second batch."
```

---
### Task 5: 验证与清理

- [ ] **Step 1: 确认没有遗漏的 `context_fifo_threshold=0`**

Run: `grep -rn "context_fifo_threshold=0" niu_api/compat.py agent/runner.py`
Expected: 无输出（0 结果）

- [ ] **Step 2: 确认 `_split_dream_first_batch` 被正确调用**

Run: `grep -rn "_split_dream_first_batch" niu_api/compat.py agent/runner.py`
Expected: compat.py 3 处（1 个定义 + sleep 调用 + force 调用）+ runner.py 1 处（force 调用）= 4 处

- [ ] **Step 3: 运行全部测试**

Run: `python/bin/python -m pytest tests/test_protect_range.py tests/test_sep_cleanup.py tests/test_dream_split.py -v`
Expected: PASS (all tests)

- [ ] **Step 4: 确认 Python 语法正确**

Run: `python/bin/python -c "from niu_api.compat import _split_dream_first_batch, _find_protected_range; print('import ok')"`
Expected: `import ok`

- [ ] **Step 5: 最终提交（如有未提交的变更）**

```bash
git add -A
git commit -m "chore: verify subagent FIFO + dream-evolver split implementation"
```
