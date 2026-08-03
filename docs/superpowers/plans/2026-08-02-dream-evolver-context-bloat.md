# Dream-Evolver 主动触发 + 回退分轮机制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 dream-evolver 从睡眠/强制压缩管道中被动触发改为按增量对话轮数主动触发；回退今天下午做的分轮机制（`_split_dream_first_batch` + batch1/batch2 逻辑）；预注入脑区列表 + 提示词优化减少工具调用轮次。

**Architecture:** (1) 回退 `_split_dream_first_batch` 及 3 处 batch1/batch2 调用代码，恢复为单次调用。(2) 从 tidy 管道（sleep + force）中移除 dream-evolver 步骤，保留 entity-extractor + context-manager + journal-agent。(3) 在 `_on_turn_end` 中新增 dream-evolver 触发检查：读游标 → 数增量对话轮数 → 达阈值则后台 daemon thread 启动 dream-evolver。(4) 阈值算法根据上下文窗口大小算出对话轮数阈值（保底 10 轮，无上限）。(5) 预注入脑区列表 + 提示词优化。

**Tech Stack:** Python 3.11, threading, LightRAG, pytest

---

## 问题分析

### 日志数据（2026-08-02 睡眠触发）

dream-evolver 处理 11 条消息（第 4-14 轮），prompt_tokens 从 13,269 涨到 26,572——翻倍。增长来源：工具返回累积（52% 的上下文是 tool results），其中 `lightrag_search_entities(query="脑区", top_k=20)` 返回 6,349 字节。

### 核心思路

**按对话轮数主动触发**：每轮对话结束后（`_on_turn_end`），检查 dream 游标后的增量对话轮数。达到阈值就在后台 daemon thread 启动 dream-evolver，处理这批消息。

- 一轮对话 = 用户提问 + 模型最终解答（含中间工具调用），是 dream-evolver 的工作单元
- 按轮数触发保证对话单元完整性，不会在中间截断
- 轮数越多，dream-evolver 能看到的上下文越完整，skill 进化判断越准（多轮失败才能准确判断是否需要写新 skill）
- 与压缩管道解耦，context-manager 不用等 dream-evolver

### 阈值算法

```
raw = int((context_window * 0.5 - system_prompt_tokens) / avg_turn_tokens)
threshold = max(10, raw)  # 保底 10 轮，无上限
```

- `context_window`：200,000（默认）
- `system_prompt_tokens`：8,000（dream-evolver system prompt 估算）
- `avg_turn_tokens`：12,000（每轮对话平均 token 开销 = 消息本身 3-5K + 工具返回累积 5-10K）
- 计算：(100,000 - 8,000) / 12,000 = 7.7 → max(10, 7) = **10 轮**
- 保底 10 轮，无上限
- 128K 窗口 → (64K - 8K) / 12K = 4.7 → max(10, 4) = 10 轮（FIFO 保底兜着）
- 200K 窗口也是 10 轮——因为 dream-evolver 的工作单元是轮，10 轮的工具调用累积在任何窗口下都安全

### 分轮机制回退

今天下午做的分轮机制提交链（5 个提交）：
- `a66c5c33` — 新增 `_split_dream_first_batch` 函数
- `d472024b` — FIFO 保底（`context_fifo_threshold 0→-1`）← **保留，不回退**
- `c0f6133f` — sleep 路径分批
- `2fd4c844` — force 路径分批（compat.py）
- `60f27b79` — force 路径分批（runner.py）

后续修复提交也会被一起回退（cursor write order、overflow gate、clamp cursor 等），因为这些修复是针对分批逻辑的。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|---------|
| `niu_api/compat.py` | 删除 `_split_dream_first_batch`；回退 sleep/force 路径为单次调用；从 tidy 管道移除 dream-evolver 步骤 | 修改 |
| `agent/runner.py` | 回退 force 路径为单次调用；从 force 管道移除 dream-evolver 步骤；`_on_turn_end` 新增触发检查 | 修改 |
| `agent/subagent.py` | `build_subagent_system_segments` 为 dream-evolver 预注入脑区列表 | 修改 |
| `config/agents/dream-evolver.md` | 脑区步骤改为参考预注入列表；增加一轮多工具指导 | 修改 |
| `tests/test_dream_split.py` | 删除（分轮机制已回退） | 删除 |
| `tests/test_dream_trigger.py` | 新建，测试阈值算法和触发逻辑 | 新建 |

---

## Task 1: 回退分轮机制

**Files:**
- Modify: `niu_api/compat.py`
- Modify: `agent/runner.py`
- Delete: `tests/test_dream_split.py`

### 设计

用 `git checkout` 从分轮前的提交恢复 3 个文件的 dream 相关代码，但保留 FIFO 保底（`context_fifo_threshold=-1`）。

分轮前的关键提交：
- `c0f6133f^`（即 `a66c5c33`）：compat.py sleep 路径原始状态 + 无 `_split_dream_first_batch`
- `2fd4c844^`（即 `c0f6133f`）：compat.py force 路径原始状态
- `60f27b79^`（即 `2fd4c844`）：runner.py force 路径原始状态

但不能直接 git checkout 整个文件——后续有其他提交改了这些文件的非 dream 部分。需要手动回退 dream 相关代码段。

- [ ] **Step 1: 删除 `tests/test_dream_split.py`**

```bash
cd /Users/lilei/tools/ai-bot && rm tests/test_dream_split.py
```

- [ ] **Step 2: 删除 `_split_dream_first_batch` 函数**

在 `niu_api/compat.py` 中删除 `_split_dream_first_batch` 函数（约 L359-432）。函数签名：

```python
def _split_dream_first_batch(
    messages: list,
    dream_msg_ids: list[str],
    msg_tokens: list[int],
    context_window_tokens: int,
    threshold: float = 0.50,
) -> list[str] | None:
```

删除整个函数定义（从 `def _split_dream_first_batch(` 到函数末尾的 `return first_batch`）。

- [ ] **Step 3: 回退 compat.py sleep 路径 dream 代码为单次调用**

将 sleep 路径中 dream-evolver 块（从 `# 2/3. dream-evolver` 到 `# 2.5/3. journal-agent` 之前）回退为分轮前的单次调用逻辑。

**当前代码结构**（分轮后，需要替换）：
```python
            # 2/3. dream-evolver（增量 task 方式）
            ...（包含 _split_dream_first_batch 调用 + batch1/batch2 逻辑，约 220 行）...
```

**替换为**（分轮前的单次调用逻辑，但保留 FIFO 保底 `context_fifo_threshold=-1`）：
```python
            # 2/3. dream-evolver（增量 task 方式）
            # 串行执行：重新获取消息列表（Entity 可能已修改 DB）
            messages = await store.get_messages()
            msg_tokens = []
            try:
                from agent.token_calculator import TokenCalculator
                calc = TokenCalculator.get()
                for msg in messages:
                    try:
                        t = calc.count_message_single(msg.role, msg.content or "", tool_calls=msg.tool_calls)
                    except Exception:
                        t = max(1, len(msg.content or "") // 2) + 4
                    msg_tokens.append(t)
            except ImportError:
                msg_tokens = [max(1, len(msg.content or "") // 2) + 4 for msg in messages]
            dream_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_msg_ids, msg_tokens
            )
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_task_prompt = """对以上消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
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
                        context_fifo_threshold=-1,  # FIFO 保底
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Dream-evolver result: {dream_result[:200]}")

                # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动，下次重跑相同范围
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
            else:
                logger.info("[Tidy] dream-evolver: no new messages since cursor")
                new_dream_id = last_dream_evolve_id
```

- [ ] **Step 4: 回退 compat.py force 路径 dream 代码为单次调用**

将 force 路径中 dream-evolver 块回退为分轮前的单次调用逻辑。

**替换为**（保留 FIFO 保底 + 新措辞 `@end` + `processed_up_to`）：
```python
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标，防止 overflow 时未定义
            if dream_force_msg_ids:
                dream_force_prompt = """对以上消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history
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
                        context_fifo_threshold=-1,  # FIFO 保底
                    )

                dream_result = await asyncio.to_thread(run_dream_evolver_force)
                if is_stop_requested():
                    logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
                    clear_stop()
                    return {"status": "aborted", "message": "Stopped by user"}
                logger.info(f"[Tidy] Force: dream-evolver completed, length={len(dream_result)}")

                # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动
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
                logger.info("[Tidy] Force: dream-evolver no incremental messages")
                new_dream_id = last_dream_evolve_id  # 无增量时保留旧游标，避免 UnboundLocalError

            # 校验游标
            if new_dream_id:
                fresh_msgs = await store.get_messages()
                fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                if new_dream_id not in fresh_ids:
                    logger.warning(f"[Tidy] Force: Dream cursor {new_dream_id} deleted by sub-agent, reverting to {last_dream_evolve_id}")
                    new_dream_id = last_dream_evolve_id
                    if new_dream_id and new_dream_id not in fresh_ids:
                        new_dream_id = ""

            if new_dream_id:
                _write_cursor_with_lock(dream_cursor_path, {
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                })
```

- [ ] **Step 5: 回退 runner.py force 路径 dream 代码为单次调用**

将 runner.py 中 dream-evolver force 路径回退为分轮前的单次调用逻辑：

**替换为**：
```python
            new_dream_id = last_dream_evolve_id
            dream_force_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Runner] Force: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = """对以上消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history + idx_to_id 映射
                _id_set = set(dream_force_msg_ids)
                dream_force_incremental_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                _, new_dream_id = self._run_subagent_step(
                    "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                    dream_force_prompt, llm_config, last_dream_evolve_id,
                    dream_force_msg_ids, "last_evolve_at",
                    history=dream_force_history, context_fifo_threshold=-1,  # FIFO 保底
                    idx_to_id=dream_force_idx_to_id,
                )

                if is_stop_requested():
                    logger.warning("[Runner] Stop requested, aborting force compress")
                    return
            else:
                logger.info("[Runner] Force: dream-evolver no incremental messages")
```

- [ ] **Step 5.5: 移除 runner.py 中 `_split_dream_first_batch` 的 import**

在 `agent/runner.py` 的 `_on_context_high_usage` 方法中，import 块（约 L1134-1145）有一行：

```python
            _split_dream_first_batch,   # 新增：dream-evolver 第一批拆分
```

删除这一行。删除后 import 块变为：

```python
        from niu_api.compat import (
            _build_compress_history,
            _build_force_prompt,
            _build_incremental_msg_text,
            _build_journal_task,
            _build_plain_history,
            _is_subagent_overflow,
            _parse_idx_list,
            _strip_analysis,
            _write_cursor_with_lock,
        )
```

- [ ] **Step 6: 确认无 `_split_dream_first_batch` 残留**

```bash
cd /Users/lilei/tools/ai-bot && grep -rn "_split_dream_first_batch" agent/ niu_api/ tests/
```
Expected: 无输出

- [ ] **Step 7: 运行全部测试确认无回归**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: PASS（test_dream_split.py 已删除，其他测试通过）

- [ ] **Step 8: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add -A && git commit -m "revert: remove _split_dream_first_batch and batch1/batch2 logic

Reverts the dream-evolver splitting mechanism added today (commits
a66c5c33, c0f6133f, 2fd4c844, 60f27b79 and their fixes).
Restores single-call dream-evolver in sleep/force paths.
Keeps FIFO fallback (context_fifo_threshold=-1, commit d472024b).
Prepares for proactive trigger by turn count."
```

---

## Task 2: 从 tidy 管道移除 dream-evolver 步骤

**Files:**
- Modify: `niu_api/compat.py`（sleep 路径 L2464 和 force 路径 L3325）
- Modify: `agent/runner.py`（force 路径 L1250 附近）

### 设计

dream-evolver 改为由 `_on_turn_end` 主动触发，不再在 tidy 管道中运行。从 sleep 和 force 两条路径中移除 dream-evolver 步骤，保留 entity-extractor → context-manager → journal-agent 的顺序。

- [ ] **Step 1: 修改 compat.py sleep 路径注释和步骤编号**

将 `# Sleep mode: entity-extractor (增量) → dream-evolver (增量) → context-manager (增量)` 改为 `# Sleep mode: entity-extractor (增量) → context-manager (增量)`

- [ ] **Step 2: 删除 compat.py sleep 路径中 dream-evolver 整段代码**

从 `# 2/3. dream-evolver（增量 task 方式）` 到 `# 2.5/3. journal-agent` 之前的 dream 代码块全部删除。

**关键**：删除后，在原 dream 代码块位置补一行兜底赋值，因为后续 context-manager 代码段引用了 `new_dream_id` 变量：

```python
            # dream-evolver 已移至 _on_turn_end 主动触发，此处保留游标基准
            new_dream_id = last_dream_evolve_id
```
- [ ] **Step 3: 修改 compat.py force 路径注释和步骤编号**

将 `# Force mode: entity-extractor 全量 → dream-evolver 全量 → context-manager 强制压缩` 改为 `# Force mode: entity-extractor 全量 → context-manager 强制压缩`

- [ ] **Step 4: 删除 compat.py force 路径中 dream-evolver 整段代码**

从 `# 2/3. dream-evolver（增量 task 方式，force 模式也是增量）` 到 `# 2.5/3. journal-agent` 之前的 dream 代码块全部删除。

**关键**：删除后，在原 dream 代码块位置补一行兜底赋值，因为后续 context-manager 代码段引用了 `new_dream_id` 变量：

```python
            # dream-evolver 已移至 _on_turn_end 主动触发，此处保留游标基准
            new_dream_id = last_dream_evolve_id
```

- [ ] **Step 5: 删除 runner.py force 路径中 dream-evolver 整段代码**

从 `# === 步骤 2/4: dream-evolver` 到 `# === 步骤 2.5/4: journal-agent` 之前的 dream 代码块全部删除。

**关键**：删除后，在原 dream 代码块位置补一行兜底赋值，因为后续 context-manager 代码段引用了 `new_dream_id` 变量：

```python
            # dream-evolver 已移至 _on_turn_end 主动触发 ===
            new_dream_id = last_dream_evolve_id
```

- [ ] **Step 6: 确认无 dream-evolver 调用代码在 tidy 管道中残留**

```bash
cd /Users/lilei/tools/ai-bot && grep -n 'call_subagent_with_auto_answer.*dream-evolver\|dream_force_prompt\|dream_task_prompt' niu_api/compat.py agent/runner.py
```
Expected: 无输出（dream-evolver 调用代码已从 tidy 管道移除）

**注意**：`dream_cursor_path`、`last_dream_evolve_id`、`new_dream_id` 等游标初始化代码仍保留——它们被 `_on_turn_end` 触发逻辑和 context-manager 引用。grep 只检查 tidy 管道中的 dream 调用代码，不检查游标基础设施。

- [ ] **Step 7: 运行全部测试**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: PASS

- [ ] **Step 8: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add -A && git commit -m "refactor: remove dream-evolver from tidy pipeline (sleep + force paths)

dream-evolver is now proactively triggered by _on_turn_end based on
incremental turn count, no longer runs inside the tidy pipeline."
```

---

## Task 3: 新增阈值算法 `_calc_dream_trigger_threshold`

**Files:**
- Create: `tests/test_dream_trigger.py`
- Modify: `agent/runner.py`（新增函数）

### 设计

根据上下文窗口大小计算 dream-evolver 触发阈值（增量对话轮数）。

一轮对话 = 用户提问 + 模型最终解答（含中间工具调用），是 dream-evolver 的工作单元。按轮数触发保证对话单元完整性，不会在中间截断。

```python
def _calc_dream_trigger_threshold(context_window_tokens: int) -> int:
    """根据上下文窗口大小计算 dream-evolver 触发阈值（增量对话轮数）。

    算法：
    - 可用预算 = context_window * 0.5（目标实际上下文使用不超过 50%）
    - 减去 system prompt 开销 ≈ 8000 tokens
    - 每轮对话平均开销 ≈ 12000 tokens（消息本身 3-5K + 工具返回累积 5-10K）
    - 阈值 = max(10, 预算 / 每轮开销)
    - 保底 10 轮，无上限

    200K 窗口 → (100K - 8K) / 12K = 7.7 → max(10, 7) = 10
    2M 窗口 → (1M - 8K) / 12K = 82.7 → 82
    """
    SYSTEM_PROMPT_TOKENS = 8000
    AVG_TURN_TOKENS = 12000
    SAFETY_RATIO = 0.5
    MIN_TURNS = 10  # 保底 10 轮，保证对话单元完整性

    if context_window_tokens <= 0:
        return MIN_TURNS  # 默认值

    budget = context_window_tokens * SAFETY_RATIO - SYSTEM_PROMPT_TOKENS
    raw_threshold = int(budget / AVG_TURN_TOKENS)

    return max(MIN_TURNS, raw_threshold)
```

- [ ] **Step 1: 写失败测试**

创建 `tests/test_dream_trigger.py`：

```python
"""Tests for dream-evolver proactive trigger threshold calculation."""

from agent.runner import _calc_dream_trigger_threshold


class TestCalcDreamTriggerThreshold:
    """Tests for _calc_dream_trigger_threshold."""

    def test_default_200k_window(self):
        """200K context window → (100000 - 8000) / 12000 = 7.7 → max(10, 7) = 10
        """
        result = _calc_dream_trigger_threshold(200000)
        assert result == 10

    def test_small_window(self):
        """32K context window → (16000 - 8000) / 12000 = 0.67 → max(10, 0) = 10
        """
        result = _calc_dream_trigger_threshold(32000)
        assert result == 10

    def test_zero_window_returns_default(self):
        """Zero or negative context window → default 10."""
        assert _calc_dream_trigger_threshold(0) == 10
        assert _calc_dream_trigger_threshold(-1) == 10

    def test_large_window_no_upper_clamp(self):
        """2M context window → (1000000 - 8000) / 12000 = 82.7 → 82 (no upper clamp).
        """
        result = _calc_dream_trigger_threshold(2000000)
        assert result == 82

    def test_medium_window(self):
        """500K context window → (250000 - 8000) / 12000 = 20.2 → 20.
        """
        result = _calc_dream_trigger_threshold(500000)
        assert result == 20
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py -v`
Expected: FAIL with `ImportError: cannot import name '_calc_dream_trigger_threshold'`

- [ ] **Step 3: 在 runner.py 中实现函数**

在 `agent/runner.py` 的 `NiuRunner` 类之前（模块级函数，靠近其他辅助函数），新增：

```python
def _calc_dream_trigger_threshold(context_window_tokens: int) -> int:
    """根据上下文窗口大小计算 dream-evolver 触发阈值（增量对话轮数）。

    算法：
    - 可用预算 = context_window * 0.5（目标实际上下文使用不超过 50%）
    - 减去 system prompt 开销 ≈ 8000 tokens
    - 每轮对话平均开销 ≈ 12000 tokens（消息本身 3-5K + 工具返回累积 5-10K）
    - 阈值 = max(10, 预算 / 每轮开销)
    - 保底 10 轮，无上限

    200K 窗口 → (100K - 8K) / 12K = 7.7 → max(10, 7) = 10
    2M 窗口 → (1M - 8K) / 12K = 82.7 → 82
    """
    SYSTEM_PROMPT_TOKENS = 8000
    AVG_TURN_TOKENS = 12000
    SAFETY_RATIO = 0.5
    MIN_TURNS = 10  # 保底 10 轮，保证对话单元完整性

    if context_window_tokens <= 0:
        return MIN_TURNS  # 默认值

    budget = context_window_tokens * SAFETY_RATIO - SYSTEM_PROMPT_TOKENS
    raw_threshold = int(budget / AVG_TURN_TOKENS)

    return max(MIN_TURNS, raw_threshold)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py -v`
Expected: PASS（5 tests）

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add agent/runner.py tests/test_dream_trigger.py && git commit -m "feat: add _calc_dream_trigger_threshold — turn count threshold from context window"
```

---

## Task 4: `_on_turn_end` 新增 dream-evolver 触发检查

**Files:**
- Modify: `agent/runner.py:870-890`（`_on_turn_end` 方法）

### 设计

在 `_on_turn_end` 中新增 dream-evolver 触发检查。每轮对话结束后：
1. 读 dream 游标
2. 从 DB 获取消息，数游标后的增量对话轮数
3. 达到阈值 → 后台 daemon thread 启动 dream-evolver
4. 用 `threading.Event` 防止并发启动多个 dream-evolver

- [ ] **Step 1: 修改 `_on_turn_end` 新增 dream 触发逻辑**

在 `agent/runner.py` 的 `_on_turn_end` 方法中，在脑区衰减之后新增 dream 触发检查。

在 `__init__` 中初始化（找到 `self._forced_sync_running = threading.Event()` 附近）：

```python
        self._dream_running = threading.Event()  # dream-evolver 后台运行标志，避免并发启动
```

修改 `_on_turn_end`：

```python
    def _on_turn_end(self, messages: list, tools_schema: list, turn: int) -> list:
        """每轮循环结束后的清理工作（动态注入已移到 _on_before_llm）。

        保留：
        - 脑区衰减 decay_all：每轮降低脑区激活级别
        - dream-evolver 触发检查：增量消息达阈值则后台启动
        """
        # Decay brain region activation levels
        try:
            from agent.brain_tools import get_activation_mgr
            mgr = get_activation_mgr()
            if mgr is not None:
                mgr.decay_all()
        except Exception as e:
            logger.debug(f"Brain region decay failed: {e}")

        # dream-evolver 触发检查：增量消息达阈值则后台启动
        self._maybe_trigger_dream_evolver()

        # No schema refresh — tools_schema stays base + disk
        return tools_schema
```

新增方法（在 `_on_turn_end` 之后）：

```python
    def _maybe_trigger_dream_evolver(self):
        """检查 dream 游标后的增量对话轮数，达阈值则后台启动 dream-evolver。"""
        # 防止并发启动
        if self._dream_running.is_set():
            return

        try:
            from pathlib import Path
            niu_dir = Path.home() / ".niu"
            dream_cursor_path = niu_dir / "last_dream_evolve.json"
            last_dream_evolve_id = ""
            if dream_cursor_path.exists():
                import json
                try:
                    # 用与 _write_cursor_with_lock 一致的 .lock 文件加锁，防止读写竞态
                    lock_path = dream_cursor_path.with_suffix(".lock")
                    with open(lock_path, "w") as lock_f:
                        from niu_api.compat import _flock, _funlock
                        _flock(lock_f)
                        try:
                            cursor_data = json.loads(dream_cursor_path.read_text(encoding="utf-8"))
                            last_dream_evolve_id = cursor_data.get("last_dream_evolve_id", "")
                        finally:
                            _funlock(lock_f)
                except Exception:
                    last_dream_evolve_id = ""

            # 从 DB 获取消息
            db_messages = self._sync_get_messages()
            if not db_messages:
                return

            # 数游标后的增量对话轮数（一轮 = 两条 user 消息之间的所有消息）
            if last_dream_evolve_id:
                cursor_idx = -1
                for i, msg in enumerate(db_messages):
                    if (getattr(msg, "id", "") or "") == last_dream_evolve_id:
                        cursor_idx = i
                        break
                incremental_msgs = db_messages[cursor_idx + 1:] if cursor_idx >= 0 else db_messages
            else:
                incremental_msgs = db_messages

            # 计算轮数：每遇到一条 role=user 消息算一轮开始
            turn_count = sum(1 for msg in incremental_msgs if getattr(msg, "role", "") == "user")

            # 计算阈值
            from agent.subagent import _read_context_window_tokens
            context_window = _read_context_window_tokens()
            threshold = _calc_dream_trigger_threshold(context_window)

            if turn_count < threshold:
                return

            logger.info(f"[Dream] Triggering dream-evolver: {turn_count} turns >= threshold {threshold}")

            # 后台启动 dream-evolver
            self._dream_running.set()
            threading.Thread(
                target=self._run_dream_evolver_background,
                daemon=True,
                name="dream-evolver-bg"
            ).start()
        except Exception as e:
            logger.warning(f"[Dream] Trigger check failed: {e}")

    def _run_dream_evolver_background(self):
        """后台运行 dream-evolver，处理游标后的增量消息。"""
        try:
            from pathlib import Path
            import json
            from agent.subagent import call_subagent_with_auto_answer
            from niu_api.compat import (
                _build_plain_history,
                _build_incremental_msg_text,
                _parse_processed_up_to,
                _write_cursor_with_lock,
                _is_subagent_overflow,
            )

            niu_dir = Path.home() / ".niu"
            dream_cursor_path = niu_dir / "last_dream_evolve.json"
            last_dream_evolve_id = ""
            if dream_cursor_path.exists():
                try:
                    lock_path = dream_cursor_path.with_suffix(".lock")
                    with open(lock_path, "w") as lock_f:
                        from niu_api.compat import _flock, _funlock
                        _flock(lock_f)
                        try:
                            cursor_data = json.loads(dream_cursor_path.read_text(encoding="utf-8"))
                            last_dream_evolve_id = cursor_data.get("last_dream_evolve_id", "")
                        finally:
                            _funlock(lock_f)
                except Exception:
                    last_dream_evolve_id = ""

            db_messages = self._sync_get_messages()
            msg_tokens = self._recalc_msg_stats(db_messages)

            dream_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_dream_evolve_id, dream_msg_ids, msg_tokens
            )

            if not dream_msg_ids:
                return

            # 构造增量 history
            _id_set = set(dream_msg_ids)
            dream_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
            dream_history, dream_idx_to_id = _build_plain_history(dream_msgs)

            dream_prompt = """对以上消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

            llm_config = self.llm_config

            dream_result = call_subagent_with_auto_answer(
                agent_name="dream-evolver",
                task=dream_prompt,
                llm_config=llm_config,
                mcp_client=None,
                history=dream_history,
                context_fifo_threshold=-1,  # FIFO 保底
            )

            # 检查停止请求（is_stop_requested/clear_stop 是 agent.runner 模块级函数，直接调用）
            if is_stop_requested():
                logger.info("[Dream] Stop requested, aborting background dream-evolver")
                clear_stop()
                return

            # 游标推进
            new_dream_id = last_dream_evolve_id
            if _is_subagent_overflow(dream_result):
                logger.warning(f"[Dream] Background dream-evolver overflow")
                # overflow 兜底：推进游标到增量消息的前 1/3 位置，避免全量重跑死循环
                # （首次部署积压大量消息时，overflow 不推进会导致无限循环）
                if len(dream_msg_ids) > 10:
                    _fallback_idx = len(dream_msg_ids) // 3
                    new_dream_id = dream_msg_ids[_fallback_idx]
                    logger.info(f"[Dream] Overflow fallback: advancing cursor to 1/3 ({_fallback_idx}/{len(dream_msg_ids)})")
            else:
                _processed_idx = _parse_processed_up_to(dream_result)
                if _processed_idx is not None and _processed_idx in dream_idx_to_id:
                    new_dream_id = dream_idx_to_id[_processed_idx]
                    logger.info(f"[Dream] Cursor advanced: {new_dream_id}")
                elif dream_msg_ids:
                    new_dream_id = dream_msg_ids[-1]
                    logger.info(f"[Dream] Cursor fallback to range end: {new_dream_id}")

            # 游标校验（双重检查，与 compat.py tidy 管道一致）
            if new_dream_id:
                fresh_msgs = self._sync_get_messages()
                fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                if new_dream_id not in fresh_ids:
                    new_dream_id = last_dream_evolve_id
                    if new_dream_id and new_dream_id not in fresh_ids:
                        new_dream_id = ""

            if new_dream_id:
                from datetime import datetime
                _write_cursor_with_lock(dream_cursor_path, {
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                })
                logger.info(f"[Dream] Cursor written: {new_dream_id}")

        except Exception as e:
            logger.error(f"[Dream] Background dream-evolver failed: {e}")
        finally:
            self._dream_running.clear()
```

- [ ] **Step 2: 确认 `_read_context_window_tokens` 在 subagent.py 中可导入**

```bash
cd /Users/lilei/tools/ai-bot && grep -n "def _read_context_window_tokens" agent/subagent.py
```
Expected: 找到函数定义

- [ ] **Step 3: 运行全部测试**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: PASS

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add agent/runner.py && git commit -m "feat: proactive dream-evolver trigger in _on_turn_end by turn count"
```

---

## Task 5: 预注入脑区列表到 dream-evolver system prompt

**Files:**
- Modify: `agent/subagent.py:483-520`（`build_subagent_system_segments`）

### 设计

在 `build_subagent_system_segments` 的步骤 4（注入 @niu-agent/@end 守则）之前，为 dream-evolver 注入当前脑区列表。复用 `get_brain_regions()` 函数。

- [ ] **Step 1: 修改 `build_subagent_system_segments`**

在 `agent/subagent.py` 的 `build_subagent_system_segments` 函数中，步骤 3 和步骤 4 之间插入脑区注入逻辑：

```python
    # 3.5 为 dream-evolver 预注入当前脑区列表（避免每轮 lightrag_search_entities 查脑区）
    if agent_name == "dream-evolver":
        try:
            from niu_api.internal.lightrag_manager import get_brain_regions
            brain_regions = get_brain_regions()
            if brain_regions:
                region_list = "、".join(brain_regions)
                static_system += f"\n\n## 当前脑区列表（预注入，无需搜索）\n\n{region_list}\n\n创建实体时直接参考以上脑区列表选择归属，不要调用 lightrag_search_entities 查询脑区。"
        except Exception:
            pass  # 获取失败不影响主流程
```

- [ ] **Step 2: 运行全部测试**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: PASS

- [ ] **Step 3: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add agent/subagent.py && git commit -m "feat: pre-inject brain region list into dream-evolver system prompt"
```

---

## Task 6: 修改 dream-evolver 提示词 — 脑区步骤 + 一轮多工具指导

**Files:**
- Modify: `config/agents/dream-evolver.md`

### 设计

1. **脑区关联步骤**：改为"参考 system prompt 中预注入的脑区列表"，删除"先检索现有脑区"的指示
2. **新增"一轮多工具"指导**：指导 dream-evolver 尽量一轮调多个工具，减少对话轮次

- [ ] **Step 1: 修改脑区操作步骤（L118-123 附近）**

将：
```
**你的操作**：
- 创建实体时，**先检索现有脑区**：`lightrag_search_entities(query="脑区", top_k=20)`
- 如果实体适合某个已有脑区（包括算法自动生成的），就连到那个脑区
- 如果没有合适的脑区，连到默认脑区（按来源选：聊天→聊天历史，文档→文档库，技能→知识体系）
- **不要手动创建新脑区**——同类实体连到默认脑区多了以后，Leiden 算法会自动聚类成新脑区
- 这形成正反馈：你连得越精准 → Leiden 发现的社区质量越高 → 下次你有更丰富的脑区可选
```

改为：
```
**你的操作**：
- 创建实体时，**直接参考 system prompt 中预注入的「当前脑区列表」**选择归属，不要调用 `lightrag_search_entities` 查询脑区
- 如果实体适合某个已有脑区（包括算法自动生成的），就连到那个脑区
- 如果没有合适的脑区，连到默认脑区（按来源选：聊天→聊天历史，文档→文档库，技能→知识体系）
- **不要手动创建新脑区**——同类实体连到默认脑区多了以后，Leiden 算法会自动聚类成新脑区
```

- [ ] **Step 2: 修改阶段B步骤3脑区关联（L181-186 附近）**

将：
```
3. **脑区关联**：将实体关联到最合适的脑区
   - **先检索现有脑区**：`lightrag_search_entities(query="脑区", top_k=20)` 获取所有脑区节点
   - **判断归属**：看当前实体是否属于某个已有脑区（如已有"Python开发脑区"，新实体"FastAPI"就属于它）
   - **适合就连**：`lightrag_insert_relation(src_id="Python开发脑区", tgt_id="FastAPI", relation="包含")`
   - **不适合不强求**：没有合适的脑区时，连到默认脑区（聊天提及→`聊天历史脑区`，文档产生→`文档库脑区`，技能工具→`知识体系脑区`）
   - **不要手动创建新脑区**——同类实体连到默认脑区多了以后，Leiden 社区发现算法会自动把它们聚类成新脑区
```

改为：
```
3. **脑区关联**：将实体关联到最合适的脑区
   - **参考 system prompt 中预注入的「当前脑区列表」**，不要调用 `lightrag_search_entities` 查询脑区
   - **判断归属**：看当前实体是否属于某个已有脑区（如已有"Python开发脑区"，新实体"FastAPI"就属于它）
   - **适合就连**：`lightrag_insert_relation(src_id="Python开发脑区", tgt_id="FastAPI", relation="包含")`
   - **不适合不强求**：没有合适的脑区时，连到默认脑区（聊天提及→`聊天历史脑区`，文档产生→`文档库脑区`，技能工具→`知识体系脑区`）
```

- [ ] **Step 2.5: 修改阶段B步骤4脑区归入（L188-194 附近）**

将：
```
4. **脑区归入**（最后做）：将实体归入对应脑区
   - `lightrag_insert_relation(src_id="脑区名", tgt_id=entity, relation="包含")`
   - 先用 `lightrag_search_entities` 查找实体应归入哪个脑区
```

改为：
```
4. **脑区归入**（最后做）：将实体归入对应脑区
   - `lightrag_insert_relation(src_id="脑区名", tgt_id=entity, relation="包含")`
   - 参考 system prompt 中预注入的「当前脑区列表」选择脑区，不要调用 `lightrag_search_entities`
```

- [ ] **Step 3: 在"工作流程"段落前新增"工具使用效率"指导**

在 `## 工作流程`（约 L142）之前插入新段落：

````text
## 工具使用效率（重要）

你的上下文窗口有限，每轮工具调用的返回结果会累积在上下文中。为减少上下文膨胀：

1. **一轮调多个工具**：如果多个工具调用之间没有依赖关系（如查多个实体是否已存在），在同一个回复中一次性调用所有工具，不要逐个调用
2. **先批量搜索再批量写入**：阶段A提取实体后，一次性搜索所有实体（多个 `lightrag_search_entities` 并行调用），确认哪些已存在，再一次性做 insert/edit
3. **避免重复搜索**：同一个实体不要搜索两次。搜索结果在上下文中可以看到，不需要重新搜
4. **top_k 控制为 5**：`lightrag_search_entities` 的 `top_k` 建议 5，不要设 20，减少返回数据量

**反面示例**（逐个调用，11 轮才处理完 11 条消息）：
```
轮1: search_entities(实体A)
轮2: search_entities(实体B)
轮3: search_entities(实体C)
...
```

**正面示例**（一轮多工具，3 轮处理完）：
```
轮1: search_entities(实体A) + search_entities(实体B) + search_entities(实体C)  # 并行搜索
轮2: insert_entity(A) + insert_entity(B) + insert_relation(A→脑区) + insert_relation(B→脑区)  # 并行写入
轮3: @end 报告
```
````

- [ ] **Step 4: 运行全部测试**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/ -v 2>&1 | tail -10
```
Expected: PASS

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add config/agents/dream-evolver.md && git commit -m "feat: dream-evolver prompt — pre-injected brain regions + multi-tool-per-turn guidance"
```

---

## Self-Review

### 1. Spec coverage

- ✅ 回退分轮机制 → Task 1
- ✅ 从 tidy 管道移除 dream-evolver → Task 2
- ✅ 按对话轮数主动触发 → Task 4
- ✅ 阈值算法根据上下文窗口大小算对话轮数（保底 10 轮） → Task 3
- ✅ 每轮模型调用后做判断 → Task 4（`_on_turn_end`）
- ✅ 不需要单独进程（daemon thread）→ Task 4
- ✅ 预注入脑区列表 → Task 5
- ✅ 提示词优化减少工具调用轮次 → Task 6

### 2. Placeholder scan

- 无 TBD/TODO
- 每个步骤都有完整代码
- 测试代码完整可执行

### 3. Type consistency

- `_calc_dream_trigger_threshold(context_window_tokens: int) -> int` → Task 3 定义，Task 4 使用一致 ✓
- `self._dream_running = threading.Event()` → `__init__` 初始化，`_maybe_trigger_dream_evolver` 检查，`_run_dream_evolver_background` 清除 ✓
- `_read_context_window_tokens` 从 subagent.py 导入 → Task 4 确认可导入 ✓
- `call_subagent_with_auto_answer` 签名不变 → Task 4 调用一致 ✓
- `_build_plain_history` / `_build_incremental_msg_text` / `_parse_processed_up_to` / `_write_cursor_with_lock` / `_is_subagent_overflow` 都从 compat.py 导入 → 签名不变 ✓

### 注意事项

1. **FIFO 保底保留**：Task 1 回退分轮机制时，`context_fifo_threshold=-1`（FIFO 保底）不回退，因为它是独立于分轮的安全机制。

2. **闭包变量捕获**：`_run_dream_evolver_background` 中 `llm_config = self.llm_config` 在主线程中赋值，daemon thread 中使用。`self.llm_config` 是 dict 引用，线程安全。

3. **并发安全**：`self._dream_running` (threading.Event) 防止并发启动多个 dream-evolver。如果 dream-evolver 正在后台运行，`_on_turn_end` 跳过触发检查。

4. **游标一致性**：后台 dream-evolver 完成后写游标，下次 `_on_turn_end` 检查时读到新游标，不会重复处理。

5. **Tidy 管道兼容**：从 tidy 管道移除 dream-evolver 后，sleep/force 路径只跑 entity-extractor + context-manager + journal-agent。步骤编号从 `1/3 → 2/3 → 2.5/3` 变为 `1/2 → 2/2`（但注释修改在 Task 2 中处理）。
