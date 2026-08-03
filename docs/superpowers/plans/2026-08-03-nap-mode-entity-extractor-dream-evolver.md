# 小憩模式：恢复 tidy 管道 dream-evolver + 主动触发前置 entity-extractor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** (1) 恢复上一轮错误移除的 tidy 管道（sleep/force + runner.py force）中的 dream-evolver 步骤。(2) 在 `_on_turn_end` 主动触发的后台方法中，前置加入 entity-extractor，形成"小憩模式"。

**Architecture:**
- **两种模式**：
  - **睡眠模式**（sleep/force）：entity-extractor → dream-evolver → [模式2以上？是→journal-agent] → context-manager。完整管道，压缩前保证内容提炼和梦境进化都跑完；模式2及以上压缩前还要做日志提取。
  - **小憩模式**（`_on_turn_end` 每 N 轮）：entity-extractor → dream-evolver。简化版，只做内容提炼和梦境进化，不压缩、不提取日志。
- **Task 1-3**：恢复 tidy 管道三处 dream-evolver（撤销提交 `ba524ee2` + `6bdd769e`）
- **Task 4**：`_run_dream_evolver_background` → `_run_nap_background`，前置 entity-extractor
- **Task 5**：更新文档

**Tech Stack:** Python 3.11, threading, LightRAG, pytest

---

## 问题分析

### 两种模式定义

| 模式 | 触发条件 | 管道 | 压缩 |
|------|---------|------|------|
| 睡眠模式 | 闲置5分钟（sleep）或上下文超阈值（force） | entity-extractor → dream-evolver → [模式2以上？journal-agent] → context-manager | 是 |
| 小憩模式 | 每 N 轮对话（`_on_turn_end`） | entity-extractor → dream-evolver | 否 |

### 为什么要恢复 tidy 管道中的 dream-evolver

上一轮提交 `ba524ee2` 从 tidy 管道中移除了 dream-evolver。这是错误的：

1. **压缩前必须跑完 entity-extractor + dream-evolver**：context-manager 压缩会裁剪/删除原始对话消息。如果 dream-evolver 没跑，知识图谱中缺少对这批消息的实体精加工，压缩后这些消息被删除，dream-evolver 再也看不到它们了。
2. **dream 安全边界**：sleep 路径 context-manager 模式一用 `new_dream_id` 作为 `_end_cursor`（压缩范围不能超过 dream 游标）。如果 dream-evolver 不跑，`new_dream_id = last_dream_evolve_id`（旧值），安全边界不正确。
3. **模式2及以上压缩前必须跑 journal-agent**：模式2（sleep，usage≥50%）和模式3（force）都是半破坏性/破坏性压缩，必须先跑 journal-agent 提取工作日志，否则日志内容随压缩丢失。

### 小憩模式中 entity-extractor → dream-evolver 的顺序依赖

entity-extractor 先用 `lightrag_insert` 入库精炼文档，LightRAG LLM 自动从中提取实体。dream-evolver 再搜索这些已入库的实体做精加工。如果 dream-evolver 先跑，它自己创建的实体名可能与 entity-extractor 入库后 LLM 提取的实体名不一致——同一概念变成两个独立节点，永远无法合并（实体碎片化）。

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|---------|
| `niu_api/compat.py` | 恢复 sleep 路径 dream-evolver 步骤；恢复 force 路径 dream-evolver 步骤；恢复注释 | 修改 |
| `agent/runner.py` | 恢复 force 路径 dream-evolver 步骤；`_run_dream_evolver_background` → `_run_nap_background` + entity-extractor 前置 | 修改 |
| `docs/SYSTEM_MANUAL.md` | 更新子 Agent 表格、两种模式描述 | 修改 |
| `docs/manual-developer.md` | 更新 tidy 管道描述 | 修改 |

---

## Task 1: 恢复 tidy 管道 sleep 路径中的 dream-evolver

**Files:**
- Modify: `niu_api/compat.py`（L2389 注释 + L2461-2462 dream 代码块）

### 设计

提交 `ba524ee2` 从 sleep 路径删除了 dream-evolver 的整段代码（约80行），替换为两行兜底赋值。提交 `6bdd769e` 修改了注释。恢复这两处改动。

- [ ] **Step 1: 恢复 sleep 路径注释**

将：
```python
            # Sleep mode: entity-extractor (增量) → context-manager (增量)
```
改回：
```python
            # Sleep mode: entity-extractor (增量) → dream-evolver (增量) → context-manager (增量)
```

- [ ] **Step 2: 恢复 sleep 路径 dream-evolver 代码块**

将当前：
```python
            # dream-evolver 已移至 _on_turn_end 主动触发，此处保留游标基准
            new_dream_id = last_dream_evolve_id
```

替换为（恢复 `ba524ee2` 删除的原始代码）：
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

- [ ] **Step 3: 恢复 `6bdd769e` 修改的注释**

在 sleep 路径 dream-evolver 之后的 journal-agent / context-manager 块中，`6bdd769e` 把 `# 重新获取消息列表（Dream 可能已修改 DB）` 改成了 `# 重新获取消息列表（Entity 可能已修改 DB）`。恢复 dream-evolver 后，dream 确实可能修改 DB，改回：

将（dream-evolver 之后的那些注释）：
```python
            # 重新获取消息列表（Entity 可能已修改 DB）
```
改回：
```python
            # 重新获取消息列表（Dream 可能已修改 DB）
```

**注意**：entity-extractor 之后、dream-evolver 之前的注释保持 `（Entity 可能已修改 DB）`，只有 dream-evolver 之后的注释改为 `（Dream 可能已修改 DB）`。

- [ ] **Step 4: 语法检查**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('syntax OK')"
```
Expected: `syntax OK`

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "restore: dream-evolver in tidy sleep path

Reverts the sleep-path removal from ba524ee2.
dream-evolver must run before context-manager compression
to avoid losing entity refinement on messages that get
deleted by compression."
```

---

## Task 2: 恢复 tidy 管道 force 路径（compat.py）中的 dream-evolver

**Files:**
- Modify: `niu_api/compat.py`（force 路径注释 + dream 代码块）

### 设计

提交 `ba524ee2` 从 force 路径删除了 dream-evolver 的整段代码（约80行），替换为两行兜底赋值。需要恢复。

- [ ] **Step 1: 恢复 force 路径注释**

将：
```python
            # Force mode: entity-extractor 全量 → context-manager 强制压缩
```
改回：
```python
            # Force mode: entity-extractor 全量 → dream-evolver 全量 → context-manager 强制压缩
```

- [ ] **Step 2: 恢复 force 路径 dream-evolver 代码块**

将当前：
```python
            # dream-evolver 已移至 _on_turn_end 主动触发，此处保留游标基准
            new_dream_id = last_dream_evolve_id
```

替换为（恢复 `ba524ee2` 删除的原始代码）：
```python
            # 2/3. dream-evolver（增量 task 方式，force 模式也是增量）
            # 串行执行：重新获取消息列表
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
            dream_force_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force mode: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

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

- [ ] **Step 3: 语法检查**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('syntax OK')"
```
Expected: `syntax OK`

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add niu_api/compat.py && git commit -m "restore: dream-evolver in tidy force path (compat.py)

Reverts the force-path removal from ba524ee2.
Force compression must run entity-extractor → dream-evolver
→ journal-agent → context-manager to avoid data loss."
```

---

## Task 3: 恢复 runner.py force 路径中的 dream-evolver

**Files:**
- Modify: `agent/runner.py`（force 路径 dream 代码块 + context-manager 注释）

### 设计

提交 `ba524ee2` 从 runner.py force 路径删除了 dream-evolver 步骤（约37行），替换为一行兜底赋值。需要恢复。

- [ ] **Step 1: 恢复 runner.py force 路径 dream-evolver 代码块**

将当前：
```python
            # dream-evolver 已移至 _on_turn_end 主动触发 ===
            new_dream_id = last_dream_evolve_id
```

替换为（恢复 `ba524ee2` 删除的原始代码）：
```python
            # === 步骤 2/4: dream-evolver（增量 task 方式）===
            if is_stop_requested():
                logger.warning("[Runner] Stop requested, aborting force compress")
                return

            # 重新获取消息列表（entity 可能已修改 DB）
            db_messages = self._sync_get_messages()
            msg_tokens = self._recalc_msg_stats(db_messages)

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

- [ ] **Step 2: 恢复 `6bdd769e` 修改的 runner.py 注释**

在 context-manager force 段落中，将：
```python
            # new_dream_id = last_dream_evolve_id（游标基准，dream-evolver 已移至 _on_turn_end）
```
改回：
```python
            # new_dream_id 在 runner.py 前面 dream-evolver 阶段已算出
```

- [ ] **Step 3: 语法检查**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax OK')"
```
Expected: `syntax OK`

- [ ] **Step 4: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add agent/runner.py && git commit -m "restore: dream-evolver in runner.py force path

Reverts the runner.py force-path removal from ba524ee2.
Restores dream-evolver step before journal-agent and
context-manager in _on_context_high_usage."
```

---

## Task 4: 小憩模式 — `_run_dream_evolver_background` 改为 `_run_nap_background`，前置 entity-extractor

**Files:**
- Modify: `agent/runner.py`（`_on_turn_end` + `_maybe_trigger_dream_evolver` + `_run_dream_evolver_background` + `__init__`）

### 设计

将 dream-evolver 后台方法重命名为小憩模式，在 dream-evolver 调用之前新增 entity-extractor 调用。小憩模式是简化版的睡眠模式——只做内容提炼和梦境进化，不压缩、不提取日志。

**关键点**：
- entity-extractor 用自己的游标 `last_entity_extract.json`，dream-evolver 用 `last_dream_evolve.json`
- entity-extractor 先跑，推进自己的游标
- dream-evolver 后跑，推进自己的游标
- entity-extractor 失败不阻断 dream-evolver
- 游标机制保证不会与 tidy 管道重复处理同一段消息

- [ ] **Step 1: 在 `_run_nap_background` 中新增 entity-extractor 调用**

将 `_run_dream_evolver_background` 方法替换为 `_run_nap_background`：

```python
    def _run_nap_background(self):
        """小憩模式：后台串行执行 entity-extractor → dream-evolver。

        简化版的睡眠模式——只做内容提炼和梦境进化，不压缩、不提取日志。
        entity-extractor 先入库精炼文档（LightRAG LLM 自动提取实体），
        dream-evolver 再精加工这些已入库的实体，避免实体碎片化。
        """
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
                _extract_overflow_info,
            )

            niu_dir = Path.home() / ".niu"
            llm_config = self.llm_config
            db_messages = self._sync_get_messages()
            if not db_messages:
                return
            msg_tokens = self._recalc_msg_stats(db_messages)

            # ============================================================
            # Step 1: entity-extractor（内容提炼）
            # ============================================================
            entity_cursor_path = niu_dir / "last_entity_extract.json"
            last_entity_id = self._read_cursor(entity_cursor_path, "last_entity_extract_id")

            entity_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_entity_id, entity_msg_ids, msg_tokens
            )

            if entity_msg_ids:
                logger.info(f"[Nap] entity-extractor: {len(entity_msg_ids)} new messages since cursor")
                _id_set = set(entity_msg_ids)
                entity_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                entity_history, entity_idx_to_id = _build_plain_history(entity_msgs)

                entity_prompt = """以上是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，N 是 1-based 序号）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

                try:
                    entity_result = call_subagent_with_auto_answer(
                        agent_name="entity-extractor",
                        task=entity_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=entity_history,
                        context_fifo_threshold=-1,  # FIFO 保底
                    )
                    logger.info(f"[Nap] entity-extractor completed, length={len(entity_result)}")

                    # 游标推进
                    new_entity_id = last_entity_id
                    if _is_subagent_overflow(entity_result):
                        overflow_info = _extract_overflow_info(entity_result)
                        logger.warning(f"[Nap] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动
                    else:
                        _processed_idx = _parse_processed_up_to(entity_result)
                        if _processed_idx is not None and _processed_idx in entity_idx_to_id:
                            new_entity_id = entity_idx_to_id[_processed_idx]
                            logger.info(f"[Nap] Entity cursor advanced: {new_entity_id}")
                        elif entity_msg_ids:
                            new_entity_id = entity_msg_ids[-1]
                            logger.info(f"[Nap] Entity cursor fallback to range end: {new_entity_id}")

                    # 游标校验
                    if new_entity_id:
                        fresh_msgs = self._sync_get_messages()
                        fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        if new_entity_id not in fresh_ids:
                            new_entity_id = last_entity_id
                            if new_entity_id and new_entity_id not in fresh_ids:
                                new_entity_id = ""

                    if new_entity_id:
                        from datetime import datetime
                        _write_cursor_with_lock(entity_cursor_path, {
                            "last_entity_extract_id": new_entity_id,
                            "last_entity_extract_at": datetime.now().isoformat(),
                        })
                        logger.info(f"[Nap] Entity cursor written: {new_entity_id}")
                except Exception as e:
                    logger.error(f"[Nap] entity-extractor failed: {e}")
                    # entity-extractor 失败不阻断 dream-evolver
            else:
                logger.info("[Nap] entity-extractor: no new messages since cursor")

            # 检查停止请求
            if is_stop_requested():
                logger.info("[Nap] Stop requested after entity-extractor, skipping dream-evolver")
                clear_stop()
                return

            # ============================================================
            # Step 2: dream-evolver（梦境进化）
            # ============================================================
            dream_cursor_path = niu_dir / "last_dream_evolve.json"
            last_dream_id = self._read_cursor(dream_cursor_path, "last_dream_evolve_id")

            # 重新获取消息（entity-extractor 可能已修改 DB）
            db_messages = self._sync_get_messages()
            if not db_messages:
                return
            msg_tokens = self._recalc_msg_stats(db_messages)

            dream_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_dream_id, dream_msg_ids, msg_tokens
            )

            if not dream_msg_ids:
                logger.info("[Nap] dream-evolver: no new messages since cursor")
                return

            logger.info(f"[Nap] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
            _id_set = set(dream_msg_ids)
            dream_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
            dream_history, dream_idx_to_id = _build_plain_history(dream_msgs)

            dream_prompt = """对以上消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复中包含 `@end`，最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那个消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

            dream_result = call_subagent_with_auto_answer(
                agent_name="dream-evolver",
                task=dream_prompt,
                llm_config=llm_config,
                mcp_client=None,
                history=dream_history,
                context_fifo_threshold=-1,  # FIFO 保底
            )

            if is_stop_requested():
                logger.info("[Nap] Stop requested, aborting after dream-evolver")
                clear_stop()
                return

            # 游标推进
            new_dream_id = last_dream_id
            if _is_subagent_overflow(dream_result):
                logger.warning(f"[Nap] dream-evolver overflow")
                if len(dream_msg_ids) > 10:
                    _fallback_idx = len(dream_msg_ids) // 3
                    new_dream_id = dream_msg_ids[_fallback_idx]
                    logger.info(f"[Nap] Overflow fallback: advancing cursor to 1/3 ({_fallback_idx}/{len(dream_msg_ids)})")
            else:
                _processed_idx = _parse_processed_up_to(dream_result)
                if _processed_idx is not None and _processed_idx in dream_idx_to_id:
                    new_dream_id = dream_idx_to_id[_processed_idx]
                    logger.info(f"[Nap] Dream cursor advanced: {new_dream_id}")
                elif dream_msg_ids:
                    new_dream_id = dream_msg_ids[-1]
                    logger.info(f"[Nap] Dream cursor fallback to range end: {new_dream_id}")

            # 游标校验
            if new_dream_id:
                fresh_msgs = self._sync_get_messages()
                fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                if new_dream_id not in fresh_ids:
                    new_dream_id = last_dream_id
                    if new_dream_id and new_dream_id not in fresh_ids:
                        new_dream_id = ""

            if new_dream_id:
                from datetime import datetime
                _write_cursor_with_lock(dream_cursor_path, {
                    "last_dream_evolve_id": new_dream_id,
                    "last_evolve_at": datetime.now().isoformat(),
                })
                logger.info(f"[Nap] Dream cursor written: {new_dream_id}")

        except Exception as e:
            logger.error(f"[Nap] Background nap failed: {e}")
        finally:
            self._nap_running.clear()
```

- [ ] **Step 2: 重命名 `_maybe_trigger_dream_evolver` → `_maybe_trigger_nap` 和 `_dream_running` → `_nap_running`**

在 `agent/runner.py` 中全局替换：

| 旧名 | 新名 |
|------|------|
| `_run_dream_evolver_background` | `_run_nap_background` |
| `_maybe_trigger_dream_evolver` | `_maybe_trigger_nap` |
| `self._dream_running` | `self._nap_running` |
| `[Dream]` (日志前缀，在 `_maybe_trigger_*` 和 `_run_*_background` 中) | `[Nap]` |

**`__init__` 中**：
```python
        self._nap_running = threading.Event()  # 小憩模式后台运行标志，避免并发启动
```

**`_on_turn_end` 中**：
```python
        # 小憩模式触发检查：增量消息达阈值则后台启动 entity-extractor → dream-evolver
        self._maybe_trigger_nap()
```

**`_maybe_trigger_nap` 方法签名和 docstring**：
```python
    def _maybe_trigger_nap(self):
        """检查增量对话轮数，达阈值则后台启动小憩模式（entity-extractor → dream-evolver）。"""
        # 防止并发启动
        if self._nap_running.is_set():
            return
```

**`_maybe_trigger_nap` 内部 Thread target 和日志前缀**：
```python
            logger.info(f"[Nap] Triggering nap: {turn_count} turns >= threshold {threshold}")

            # 后台启动小憩模式
            self._nap_running.set()
            try:
                threading.Thread(
                    target=self._run_nap_background,
                    daemon=True,
                    name="nap-bg"
                ).start()
            except Exception:
                self._nap_running.clear()
                raise
        except Exception as e:
            logger.warning(f"[Nap] Trigger check failed: {e}")
```

- [ ] **Step 3: 语法检查**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax OK')"
```
Expected: `syntax OK`

- [ ] **Step 4: 确认无旧名残留**

```bash
cd /Users/lilei/tools/ai-bot && grep -n "_dream_running\|_run_dream_evolver_background\|_maybe_trigger_dream_evolver" agent/runner.py
```
Expected: 无输出

- [ ] **Step 5: 运行已有测试确认无回归**

```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py -v
```
Expected: PASS（5 tests，`_calc_dream_trigger_threshold` 函数名不改）

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add agent/runner.py && git commit -m "feat: nap mode — entity-extractor before dream-evolver in background

_run_dream_evolver_background → _run_nap_background
_maybe_trigger_dream_evolver → _maybe_trigger_nap
_dream_running → _nap_running

Nap mode is a simplified sleep mode: entity-extractor →
dream-evolver only, no compression, no journal-agent.
entity-extractor runs first to prevent entity fragmentation:
its lightrag_insert creates entities via LightRAG LLM
extraction, then dream-evolver searches and refines those
entities instead of creating its own with different names.

tidy pipeline (sleep/force) unchanged — entity-extractor +
dream-evolver still run there too. Cursors prevent
double-processing."
```

---

## Task 5: 更新文档

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`
- Modify: `docs/manual-developer.md`

### 设计

更新文档反映两种模式：睡眠模式（完整管道）和小憩模式（简化版）。

- [ ] **Step 1: 更新 SYSTEM_MANUAL.md 子 Agent 表格**

entity-extractor 触发方式改为 `auto-tidy 管线 + 小憩模式主动触发`

dream-evolver 触发方式改为 `auto-tidy 管线 + 小憩模式主动触发（_on_turn_end）`

- [ ] **Step 2: 更新 SYSTEM_MANUAL.md BLOCKED_SUBAGENTS 描述**

改为：
```
- `entity-extractor`：由睡眠模式（auto-tidy 管线）+ 小憩模式（`_on_turn_end` 按对话轮数）双重触发
- `dream-evolver`：由睡眠模式（auto-tidy 管线）+ 小憩模式（`_on_turn_end` 按对话轮数）双重触发；小憩模式中在 entity-extractor 之后串行执行
- `context-manager`：由睡眠模式（auto-tidy 管线）自动调度
```

- [ ] **Step 3: 更新 SYSTEM_MANUAL.md dream-evolver 主动触发机制段落**

将段落标题从 `### dream-evolver 主动触发机制` 改为 `### 两种模式：睡眠模式与小憩模式`

替换整个段落内容为：

```markdown
### 两种模式：睡眠模式与小憩模式

系统有两种后台处理模式，都包含 entity-extractor → dream-evolver：

**睡眠模式**（sleep/force）：完整管道，由闲置 5 分钟或上下文超阈值触发。

| 步骤 | 子 Agent | 说明 |
|------|----------|------|
| 1 | entity-extractor | 内容提炼，`lightrag_insert` 入库 |
| 2 | dream-evolver | 梦境进化，精加工实体 + 维护 skill |
| 3 | journal-agent | 日志提取（仅模式2及以上：sleep usage≥50% 或 force） |
| 4 | context-manager | 上下文压缩 |

压缩前必须保证 entity-extractor + dream-evolver 都跑完，否则压缩删除原始消息后，实体提取和图谱精加工的机会就丢失了。模式2及以上压缩前还必须跑 journal-agent，否则日志内容随压缩丢失。

**小憩模式**（`_on_turn_end`）：简化版，每 N 轮对话后台触发。

| 步骤 | 子 Agent | 说明 |
|------|----------|------|
| 1 | entity-extractor | 内容提炼，`lightrag_insert` 入库 |
| 2 | dream-evolver | 梦境进化，精加工实体 + 维护 skill |

不压缩、不提取日志。高频小批量触发比睡眠时一次性处理大量消息更安全——工具返回累积可控，上下文不会溢出。

**entity-extractor → dream-evolver 的顺序依赖**：entity-extractor 先用 `lightrag_insert` 入库精炼文档，LightRAG LLM 自动从中提取实体。dream-evolver 再搜索这些已入库的实体做精加工。如果 dream-evolver 先跑，它自己创建的实体名可能与 entity-extractor 入库后 LLM 提取的实体名不一致——同一概念变成两个独立节点，永远无法合并（实体碎片化）。

**游标去重**：entity-extractor 和 dream-evolver 在两种模式中都会跑，但各自有独立的游标文件（`last_entity_extract.json` / `last_dream_evolve.json`），处理完推进自己的游标，保证不重复处理同一段消息。
```

- [ ] **Step 4: 更新 manual-developer.md**

将 `上下文整理管道升级：entity-extractor + context-manager 两游标机制 + dream-evolver 主动触发` 改为 `上下文整理管道升级：entity-extractor + dream-evolver + context-manager + journal-agent 四游标机制 + 小憩模式主动触发（entity-extractor → dream-evolver）`

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot && git add docs/SYSTEM_MANUAL.md docs/manual-developer.md && git commit -m "docs: two modes — sleep mode (full pipeline) + nap mode (simplified)

- Sleep mode: entity-extractor → dream-evolver → journal-agent
  (mode2+) → context-manager
- Nap mode: entity-extractor → dream-evolver (no compression)
- Document compression prerequisites
- Document entity fragmentation prevention"
```

---

## Self-Review

### 1. Spec coverage

| 需求 | 对应 Task |
|------|----------|
| 恢复 tidy 管道 sleep 路径 dream-evolver | Task 1 |
| 恢复 tidy 管道 force 路径（compat.py）dream-evolver | Task 2 |
| 恢复 runner.py force 路径 dream-evolver | Task 3 |
| 小憩模式新增 entity-extractor → dream-evolver | Task 4 |
| 更新文档 | Task 5 |
| 睡眠模式：压缩前必须跑完 entity-extractor + dream-evolver | Task 1-3 恢复管道 |
| 睡眠模式：模式2及以上压缩前必须跑 journal-agent | 已有代码（sleep: usage≥50%；force: 始终） |
| 小憩模式：只做内容提炼和梦境进化，不压缩 | Task 4 |

### 2. Placeholder scan

✅ 无 TBD/TODO/placeholder。所有代码块都是完整实现。

### 3. Type consistency

| 名称 | 定义位置 | 使用位置 | 一致性 |
|------|---------|---------|--------|
| `_run_nap_background` | Task 4 Step 1 | Task 4 Step 2 (Thread target) | ✅ |
| `_maybe_trigger_nap` | Task 4 Step 2 | Task 4 Step 2 (_on_turn_end) | ✅ |
| `self._nap_running` | Task 4 Step 2 (__init__) | Task 4 Step 1 (finally) + Step 2 (trigger) | ✅ |
| `_calc_dream_trigger_threshold` | 已存在（不改名） | Task 4 Step 2 (_maybe_trigger_nap) | ✅ |
| `entity_cursor_path` | Task 4 Step 1 | Task 4 Step 1 (read + write) | ✅ |
| `dream_cursor_path` | Task 4 Step 1 | Task 4 Step 1 (read + write) | ✅ |

### 关键设计验证

1. **睡眠模式完整恢复**：Task 1-3 恢复 sleep/force/runner.py 三处 dream-evolver 步骤，管道为 entity-extractor → dream-evolver → journal-agent（模式2+）→ context-manager。✅
2. **小憩模式是简化版**：只做 entity-extractor → dream-evolver，不压缩、不提取日志。✅
3. **压缩前必须跑完 entity-extractor + dream-evolver**：恢复后管道顺序保证。✅
4. **模式2/3压缩前必须跑 journal-agent**：已有代码（sleep: usage≥50% 时跑；force: 始终跑）。✅
5. **dream 安全边界**：恢复后 `new_dream_id` 由 dream-evolver 实际推进。✅
6. **entity-extractor 失败不阻断 dream-evolver**：Task 4 Step 1 中 try/except。✅
7. **游标去重**：两种模式都跑 entity-extractor + dream-evolver，游标保证不重复。✅
