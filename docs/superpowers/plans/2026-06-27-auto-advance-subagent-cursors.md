# 子Agent游标自动推进 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将子Agent游标从"LLM返回UUID"改为"程序根据成功/失败自动推进"，消除LLM格式不合规、幻觉UUID、指向PROTECTED消息等风险。

**Architecture:** 子Agent成功完成时，游标推进到增量范围最后一条消息的UUID；overflow/失败时游标不动。保留现有的校验逻辑（UUID存在性检查、PROTECTED回退）。从prompt中移除游标报告指令。改动覆盖3条路径：compat.py睡眠模式、compat.py强制模式、runner.py强制模式，以及handler.py的journal游标。

**Tech Stack:** Python (niu_api/compat.py, agent/runner.py, agent/handler.py, config/agents/*.md)

**范围说明**：context-manager模式三的 `cursor=` idx格式解析不受此次改动影响（它不使用 `_extract_cursor_id`）。

---

## File Structure

| 文件 | 改动 | 说明 |
|------|------|------|
| `niu_api/compat.py` | 修改 | 睡眠模式+强制模式共8处游标提取→自动推进，4处prompt移除游标指令，1处 `_build_journal_task` 移除游标指令 |
| `agent/runner.py` | 修改 | `_run_subagent_step` 游标提取→自动推进 |
| `agent/handler.py` | 修改 | `_update_journal_cursor` 游标提取→自动推进 |
| `config/agents/entity-extractor.md` | 修改 | 移除游标报告指令 |
| `config/agents/dream-evolver.md` | 修改 | 移除游标报告指令 |
| `config/agents/journal-agent.md` | 修改 | 移除游标报告指令 |
| `config/agents/context-manager.md` | 修改 | 移除游标报告指令 |

**注意**：行号基于改动前的文件快照。按任务顺序执行时，前面的改动会导致后续行号偏移，必须用内容匹配（old_string/new_string）而非行号定位。

---

### Task 1: compat.py 睡眠模式 — entity-extractor 游标自动推进

**Files:**
- Modify: `niu_api/compat.py`

- [ ] **Step 1: 替换 entity-extractor 游标逻辑**

old_string:
```python
                # 游标提取和推进
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_entity_extract_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_entity_id = recovered
                        logger.info(f"[Tidy] Entity cursor recovered from partial_result: {new_entity_id}")
                    else:
                        new_entity_id = entity_msg_ids[-1]
                        logger.warning(f"[Tidy] Entity cursor overflow fallback to last incremental msg: {new_entity_id}")
                else:
                    extracted = _extract_cursor_id(entity_result, "last_entity_extract_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_entity_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_entity_id = entity_msg_ids[-1]
                        logger.warning(f"[Tidy] Entity cursor not matched or null, advancing to last incremental msg: {new_entity_id}")
```

new_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    new_entity_id = entity_msg_ids[-1]
                    logger.info(f"[Tidy] Entity cursor auto-advanced to: {new_entity_id}")
```

- [ ] **Step 2: 移除 entity-extractor prompt 中的游标报告指令**

old_string:
```python
            entity_prompt_suffix = """

处理完成后，在报告末尾用 JSON 格式报告：{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}
**必须推进游标**：即使没有可提取的内容（全是程序化操作、闲聊等），也必须输出 idx 最大的消息的 UUID。只有当传入的消息列表本身为空（一条消息都没有）时，才输出 {"last_entity_extract_id": null}"""
```

new_string:
```python
            entity_prompt_suffix = ""
```

- [ ] **Step 3: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: OK

---

### Task 2: compat.py 睡眠模式 — dream-evolver 游标自动推进

**Files:**
- Modify: `niu_api/compat.py`

- [ ] **Step 1: 替换 dream-evolver 游标逻辑**

old_string:
```python
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
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
```

new_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    new_dream_id = dream_msg_ids[-1]
                    logger.info(f"[Tidy] Dream cursor auto-advanced to: {new_dream_id}")
```

- [ ] **Step 2: 移除 dream-evolver prompt 中的游标报告指令**

old_string:
```python
                dream_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要精加工的内容，也必须输出 idx 最大的消息的 UUID。"""
```

new_string:
```python
                dream_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_msg_text}"""
```

- [ ] **Step 3: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: OK

---

### Task 3: compat.py 睡眠模式 — journal-agent 游标自动推进 + _build_journal_task 清理

**Files:**
- Modify: `niu_api/compat.py`

- [ ] **Step 1: 替换 journal-agent 游标逻辑**

old_string:
```python
                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        partial = overflow_info.get("partial_result", "")
                        recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                        if recovered and recovered != "NULL":
                            new_journal_id = recovered
                        else:
                            new_journal_id = journal_msg_ids[-1]
                            logger.warning(f"[Tidy] Journal cursor overflow fallback: {new_journal_id}")
                    else:
                        extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                        if extracted and extracted != "NULL":
                            new_journal_id = extracted
                        elif extracted == "NULL" or not extracted:
                            new_journal_id = journal_msg_ids[-1]
                            logger.warning(f"[Tidy] Journal cursor not matched, fallback: {new_journal_id}")
```

new_string:
```python
                    # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动，下次重跑相同范围
                    else:
                        new_journal_id = journal_msg_ids[-1]
                        logger.info(f"[Tidy] Journal cursor auto-advanced to: {new_journal_id}")
```

- [ ] **Step 2: 移除 `_build_journal_task` 中的游标报告指令**

old_string:
```python
    prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_journal_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。"""
```

new_string:
```python
    prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_msg_text}"""
```

- [ ] **Step 3: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: OK

---

### Task 4: compat.py 睡眠模式 — context-manager 游标自动推进

**Files:**
- Modify: `niu_api/compat.py`

- [ ] **Step 1: 移除模式一 prompt 中的游标报告指令**

old_string:
```python
                _cursor_instruction = """处理完成后，在报告末尾用 JSON 格式报告：{"last_compress_id": "<收到的消息中 idx 最大的、非 [PROTECTED] 标记的消息的 id（UUID）>"}
**必须推进游标**：即使没有需要处理的内容，也必须输出 idx 最大的非 [PROTECTED] 消息的 UUID。
**禁止将游标指向 [PROTECTED] 消息**：[PROTECTED] 消息不受你的处理范围控制，游标指向它们会导致下次增量范围卡死。"""
```

new_string:
```python
                _cursor_instruction = ""
```

- [ ] **Step 2: 替换模式一游标提取逻辑**

old_string（从 `if _is_subagent_overflow(cm_result):` 开始到 PROTECTED 回退逻辑结束）：
```python
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
                        # 防御：游标指向 PROTECTED 消息会导致下次增量范围卡死
                        if new_compress_id and protected_ids and new_compress_id in protected_ids:
                            logger.warning(f"[Tidy] Compress cursor {new_compress_id} is PROTECTED, reverting to non-protected message")
                            # 从 compress_msg_ids 中找 idx 最大的非 PROTECTED 消息
                            _pid_set = set(protected_ids)
                            new_compress_id = ""
                            for mid in reversed(compress_msg_ids):
                                if mid not in _pid_set and mid in fresh_ids:
                                    new_compress_id = mid
                                    break
```

new_string:
```python
                    # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                    if _is_subagent_overflow(cm_result):
                        overflow_info = _extract_overflow_info(cm_result)
                        logger.warning(f"[Tidy] context-manager overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动
                    else:
                        new_compress_id = compress_msg_ids[-1] if compress_msg_ids else last_compress_id
                        logger.info(f"[Tidy] Compress cursor auto-advanced to: {new_compress_id}")

                    # 校验游标
                    if new_compress_id:
                        fresh_msgs = await store.get_messages()
                        fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        if new_compress_id not in fresh_ids:
                            logger.warning(f"[Tidy] Compress cursor {new_compress_id} deleted, reverting to {last_compress_id}")
                            new_compress_id = last_compress_id
                            if new_compress_id and new_compress_id not in fresh_ids:
                                new_compress_id = ""
                        # 防御：游标指向 PROTECTED 消息会导致下次增量范围卡死
                        if new_compress_id and protected_ids and new_compress_id in protected_ids:
                            logger.warning(f"[Tidy] Compress cursor {new_compress_id} is PROTECTED, reverting to non-protected message")
                            _pid_set = set(protected_ids)
                            new_compress_id = ""
                            for mid in reversed(compress_msg_ids):
                                if mid not in _pid_set and mid in fresh_ids:
                                    new_compress_id = mid
                                    break
```

- [ ] **Step 3: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: OK

---

### Task 5: compat.py 强制模式 — entity/dream/journal 游标自动推进

**Files:**
- Modify: `niu_api/compat.py`

强制模式（`elif mode == "force":` 分支）有与睡眠模式完全相同的3组游标提取逻辑 + 3处 prompt 游标指令，需要同样的替换。

- [ ] **Step 1: 强制模式 entity-extractor prompt 移除游标指令**

old_string:
```python
            entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的内容，也必须输出 idx 最大的消息的 UUID。"""
```

new_string:
```python
            entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}"""
```

- [ ] **Step 2: 强制模式 entity-extractor 游标逻辑替换**

old_string:
```python
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
```

new_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] Force: entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动
                else:
                    new_entity_id = entity_force_msg_ids[-1] if entity_force_msg_ids else last_entity_extract_id
                    logger.info(f"[Tidy] Force: Entity cursor auto-advanced to: {new_entity_id}")
```

- [ ] **Step 3: 强制模式 dream-evolver prompt 移除游标指令**

old_string:
```python
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要精加工的内容，也必须输出 idx 最大的消息的 UUID。"""
```

new_string:
```python
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}"""
```

- [ ] **Step 4: 强制模式 dream-evolver 游标逻辑替换**

old_string:
```python
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
```

new_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动
                else:
                    new_dream_id = dream_force_msg_ids[-1]
                    logger.info(f"[Tidy] Force: Dream cursor auto-advanced to: {new_dream_id}")
```

- [ ] **Step 5: 强制模式 journal-agent 游标逻辑替换**

old_string:
```python
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    logger.warning(f"[Tidy] Force: journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_journal_id = recovered
                    else:
                        new_journal_id = journal_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Journal cursor overflow fallback: {new_journal_id}")
                else:
                    extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_journal_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_journal_id = journal_force_msg_ids[-1]
                        logger.warning(f"[Tidy] Force: Journal cursor not matched, fallback: {new_journal_id}")
```

new_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    logger.warning(f"[Tidy] Force: journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    new_journal_id = journal_force_msg_ids[-1] if journal_force_msg_ids else last_journal_id
                    logger.info(f"[Tidy] Force: Journal cursor auto-advanced to: {new_journal_id}")
```

- [ ] **Step 6: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: OK

---

### Task 6: runner.py — _run_subagent_step 游标自动推进

**Files:**
- Modify: `agent/runner.py`

- [ ] **Step 1: 替换游标提取逻辑**

old_string:
```python
        # --- cursor extraction ---
        new_cursor_id = last_cursor_id
        if _is_subagent_overflow(result):
            overflow_info = _extract_overflow_info(result)
            logger.warning(f"[Runner] Force: {step_name} overflow: {overflow_info.get('turns_completed', 0)} turns")
            partial = overflow_info.get("partial_result", "")
            recovered = _extract_cursor_id(partial, cursor_field, msg_id_set)
            if recovered and recovered != "NULL":
                new_cursor_id = recovered
            else:
                new_cursor_id = fallback_ids[-1] if fallback_ids else last_cursor_id
                logger.warning(f"[Runner] Force: {step_name} cursor overflow fallback: {new_cursor_id}")
        else:
            extracted = _extract_cursor_id(result, cursor_field, msg_id_set)
            if extracted and extracted != "NULL":
                new_cursor_id = extracted
            elif extracted == "NULL" or not extracted:
                new_cursor_id = fallback_ids[-1] if fallback_ids else last_cursor_id
                logger.warning(f"[Runner] Force: {step_name} cursor not matched, fallback: {new_cursor_id}")
```

new_string:
```python
        # --- cursor auto-advance ---
        new_cursor_id = last_cursor_id
        if _is_subagent_overflow(result):
            overflow_info = _extract_overflow_info(result)
            logger.warning(f"[Runner] Force: {step_name} overflow: {overflow_info.get('turns_completed', 0)} turns")
            # overflow 时游标不动，下次重跑相同范围
        else:
            new_cursor_id = fallback_ids[-1] if fallback_ids else last_cursor_id
            logger.info(f"[Runner] Force: {step_name} cursor auto-advanced to: {new_cursor_id}")
```

- [ ] **Step 2: 移除 `_extract_cursor_id` 导入**

old_string:
```python
        from niu_api.compat import _extract_cursor_id, _is_subagent_overflow, _extract_overflow_info, _write_cursor_with_lock
```

new_string:
```python
        from niu_api.compat import _is_subagent_overflow, _extract_overflow_info, _write_cursor_with_lock
```

- [ ] **Step 3: 清理 `_run_subagent_step` 死参数 `msg_id_set`**

`cursor_field` 在 L738 的 `_write_cursor_with_lock` 中仍被使用，**必须保留**。`msg_id_set` 在新逻辑中不再使用（原用于 `_extract_cursor_id` 的 UUID 校验，现由 L726-733 的 fresh_ids 检查替代），可以移除。

函数签名 old_string:
```python
    def _run_subagent_step(self, step_name, cursor_path, cursor_field,
                           prompt, llm_config, msg_id_set, last_cursor_id,
                           fallback_ids, timestamp_field):
```

new_string:
```python
    def _run_subagent_step(self, step_name, cursor_path, cursor_field,
                           prompt, llm_config, last_cursor_id,
                           fallback_ids, timestamp_field):
```

调用点 1（entity-extractor，约 L835-839）old_string:
```python
                _, new_entity_id = self._run_subagent_step(
                    "entity-extractor", entity_cursor_path, "last_entity_extract_id",
                    truncated_entity_prompt, llm_config, msg_id_set, last_entity_extract_id,
                    entity_force_msg_ids, "last_entity_extract_at",
                )
```

new_string:
```python
                _, new_entity_id = self._run_subagent_step(
                    "entity-extractor", entity_cursor_path, "last_entity_extract_id",
                    truncated_entity_prompt, llm_config, last_entity_extract_id,
                    entity_force_msg_ids, "last_entity_extract_at",
                )
```

调用点 2（dream-evolver，约 L874-878）old_string:
```python
                _, new_dream_id = self._run_subagent_step(
                    "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                    truncated_dream_prompt, llm_config, msg_id_set, last_dream_evolve_id,
                    dream_force_msg_ids, "last_evolve_at",
                )
```

new_string:
```python
                _, new_dream_id = self._run_subagent_step(
                    "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                    truncated_dream_prompt, llm_config, last_dream_evolve_id,
                    dream_force_msg_ids, "last_evolve_at",
                )
```

调用点 3（journal-agent，约 L905-909）old_string:
```python
                _, new_journal_id = self._run_subagent_step(
                    "journal-agent", journal_cursor_path, "last_journal_id",
                    truncated_journal_prompt, llm_config, msg_id_set, last_journal_id,
                    journal_force_msg_ids, "last_journal_at",
                )
```

new_string:
```python
                _, new_journal_id = self._run_subagent_step(
                    "journal-agent", journal_cursor_path, "last_journal_id",
                    truncated_journal_prompt, llm_config, last_journal_id,
                    journal_force_msg_ids, "last_journal_at",
                )
```

同时更新函数 docstring，移除 `msg_id_set` 的参数说明，保留 `cursor_field` 的说明。

docstring old_string:
```python
        msg_id_set : set[str]
            Set of currently-valid message IDs (for cursor validation).
        last_cursor_id : str
```

new_string:
```python
        last_cursor_id : str
```

注意：runner.py 中 journal-agent 的 prompt 通过 `_build_journal_task` 生成（L903），该函数的游标指令已在 Task 3 Step 2 中统一清理，无需在 Task 6 中额外处理。

- [ ] **Step 4: runner.py 内联 prompt 移除游标报告指令**

`_on_context_high_usage` 方法中的 `entity_force_prompt` 和 `dream_force_prompt` 包含游标报告指令，需要移除（与 compat.py 强制模式 prompt 保持一致）。

entity_force_prompt（约 L823-830）old_string:
```python
                entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有可提取的内容，也必须输出 idx 最大的消息的 UUID。"""
```

new_string:
```python
                entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}"""
```

dream_force_prompt（约 L864-869）old_string:
```python
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}

处理完成后，在报告末尾用 JSON 格式报告：{{"last_dream_evolve_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要精加工的内容，也必须输出 idx 最大的消息的 UUID。"""
```

new_string:
```python
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}"""
```

- [ ] **Step 5: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('agent/runner.py').read()); print('OK')"`
Expected: OK

---

### Task 7: handler.py — _update_journal_cursor 游标自动推进

**Files:**
- Modify: `agent/handler.py`

- [ ] **Step 1: 替换游标提取逻辑**

old_string:
```python
                # 完整 fallback 链（与 compat.py 路径2/3 一致）
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    partial = overflow_info.get("partial_result", "")
                    recovered = _extract_cursor_id(partial, "last_journal_id", msg_id_set)
                    if recovered and recovered != "NULL":
                        new_journal_id = recovered
                    else:
                        new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id
                else:
                    extracted = _extract_cursor_id(journal_result, "last_journal_id", msg_id_set)
                    if extracted and extracted != "NULL":
                        new_journal_id = extracted
                    elif extracted == "NULL" or not extracted:
                        new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id
```

new_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id
                    logger.info(f"[Handler] Journal cursor auto-advanced to: {new_journal_id}")
```

- [ ] **Step 2: 移除 `_extract_cursor_id` 导入**

old_string:
```python
        from niu_api.compat import _extract_cursor_id, _is_subagent_overflow, _extract_overflow_info
```

new_string:
```python
        from niu_api.compat import _is_subagent_overflow, _extract_overflow_info
```

- [ ] **Step 3: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('agent/handler.py').read()); print('OK')"`
Expected: OK

---

### Task 8: 子Agent提示词 — 移除游标报告指令

**Files:**
- Modify: `config/agents/entity-extractor.md`
- Modify: `config/agents/dream-evolver.md`
- Modify: `config/agents/journal-agent.md`
- Modify: `config/agents/context-manager.md`

- [ ] **Step 1: entity-extractor.md — 精简游标机制章节**

old_string:
```markdown
## 游标机制

- 程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息
- 每条消息带有 `[id:UUID] [idx:N]` 标注，idx 是全量列表序号（不是增量相对序号）
- 处理完成后报告 idx 最大的那条消息的 UUID
- 在报告末尾输出：`{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}`
- **必须推进游标**：即使没有可提取的内容（全是程序化操作、闲聊等），也必须输出 idx 最大的消息的 UUID
- 只有当传入的消息列表本身为空（一条消息都没有）时，才输出 `{"last_entity_extract_id": null}`
```

new_string:
```markdown
## 游标机制

- 程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息
- 每条消息带有 `[id:UUID] [idx:N]` 标注，idx 是全量列表序号（不是增量相对序号）
- 游标由程序自动推进，你无需报告游标位置
```

- [ ] **Step 2: dream-evolver.md — 精简游标机制章节**

old_string:
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

new_string:
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
2. 游标由程序自动推进，你无需报告游标位置

**输入规范**：
- 消息内容为**完整原文**，不做截断
- `Xtokens` 为该条消息的 token 估算值（基于完整内容计算）
- `role` 为消息角色（user / assistant / tool）
```

- [ ] **Step 3: dream-evolver.md — 删除回复格式中的游标报告行**

old_string:
```markdown
游标更新：last_dream_evolve_id = {new_cursor_id}

{如有异常或跳过，在此说明原因}
```

new_string:
```markdown
{如有异常或跳过，在此说明原因}
```

- [ ] **Step 4: dream-evolver.md — 删除回复末尾的游标 JSON 指令**

old_string:
```markdown
处理完成后，在报告末尾的回复文本中附上以下 JSON（直接写在回复里，不要写文件）：`{"last_dream_evolve_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}`

注意：游标应推进到操作范围的终点（范围内 idx 最大的那条消息的 id），而不是最后被操作的那条。游标指向的消息必须仍存在。
```

new_string:（空字符串，删除这两段文字。删除后清理多余空行，使代码块结束标记 ``` 后直接接 `## ⛔ 严格禁止` 标题。）

- [ ] **Step 5: journal-agent.md — 精简游标机制章节 + 删除游标报告指令**

游标机制章节 old_string:
```markdown
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息。

每条消息格式为 `[id:UUID] [idx:N] Xtokens role: content`。

**操作步骤**：
1. 直接处理收到的全部消息
2. 操作完成后，用 id（UUID）报告游标位置
3. 游标应推进到收到的消息中 idx 最大的那条的 id
```

new_string:
```markdown
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息。

每条消息格式为 `[id:UUID] [idx:N] Xtokens role: content`。

**操作步骤**：
1. 直接处理收到的全部消息
2. 游标由程序自动推进，你无需报告游标位置
```

回复格式中的游标报告行 old_string:
```markdown
提取条目：{n} 条工作日志
游标更新：last_journal_id = {new_cursor_id}
```

new_string:
```markdown
提取条目：{n} 条工作日志
```

末尾游标 JSON 指令 old_string:
```markdown
处理完成后，在报告末尾用 JSON 格式报告：`{"last_journal_id": "<操作范围内 idx 最大的、且仍存在的消息的 id（UUID）>"}`

**必须推进游标**：即使没有可提取的工作内容，也必须输出 idx 最大的消息的 UUID。只有当传入的消息列表本身为空时，才输出 `{"last_journal_id": null}`。
```

new_string:（空字符串，删除这两段文字。删除后清理多余空行。）

- [ ] **Step 6: context-manager.md — 精简游标报告章节**

old_string:
```markdown
## 游标报告

**模式一**：处理完成后，在**回复文本的最后一行**直接输出 JSON：`{"last_compress_id": "<游标终点消息的 id（UUID）>"}`

**注意**：这是在回复文本中输出，不是调用任何工具写入。不要使用 add_message 或任何其他工具来输出游标信息。

**模式二**：不需要报告游标（模式二无游标机制，跳过此步骤）

**模式三**：通过 cursor= 行报告（idx 格式，非 UUID），程序自动转换为 UUID

**游标终点**：
- 模式一：操作范围内 idx 最大的、且仍存在的**非 [PROTECTED]** 消息的 id
- 模式二：不需要报告游标（模式二无游标机制，跳过此步骤）
- 模式三：所有消息中 idx 最大的、且仍存在的消息的 id（因为模式三操作所有消息）

注意：
- 游标用 id（UUID）存储，因为 id 是持久化的，不受删除操作影响
- 游标应推进到操作范围的终点，而不是最后被操作的那条
- **游标指向的消息必须仍存在**：如果范围终点消息被删除，则回退到范围内仍存在的、idx 最大的消息的 id
- **禁止将游标指向 [PROTECTED] 消息**（模式一）：[PROTECTED] 消息不受你的处理范围控制，游标指向它们会导致下次增量范围卡死（游标之后的消息为空，压缩永远跳过）
```

new_string:
```markdown
## 游标报告

**模式一**：游标由程序自动推进，你无需报告游标位置。

**模式二**：不需要报告游标（模式二无游标机制，跳过此步骤）

**模式三**：通过 cursor= 行报告（idx 格式，非 UUID），程序自动转换为 UUID
```

---

### Task 9: 清理 _extract_cursor_id 函数

**Files:**
- Modify: `niu_api/compat.py`
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 确认 `_extract_cursor_id` 不再有调用者**

Run: `grep -rn "_extract_cursor_id" niu_api/ agent/ --include="*.py"`

确认所有调用已在 Task 1-7 中移除。模式三的 `cursor=` idx 解析不使用此函数。

- [ ] **Step 2: 删除函数定义**

删除 `compat.py` 中 `_extract_cursor_id` 函数定义（约 L33-66）。

- [ ] **Step 3: 清理测试文件**

`tests/test_tidy_cursor.py` 中有 `TestExtractCursorIdNull` 类（L166-213）直接测试 `_extract_cursor_id`，以及 L37 的导入。

old_string:
```python
from niu_api.compat import _extract_cursor_id
```
删除此行。

old_string:
```python
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
        """明确返回 null 时，返回特殊标记 'NULL'（区分'没报告'和'明确返回null'）"""
        result = _extract_cursor_id(
            '处理完成 {"last_entity_extract_id": null} 收尾',
            "last_entity_extract_id",
            set(),
        )
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
删除整个类。

- [ ] **Step 4: 语法检查**

Run: `python3 -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: OK

Run: `python3 -c "import ast; ast.parse(open('tests/test_tidy_cursor.py').read()); print('OK')"`
Expected: OK

---

### Task 10: 最终验证与提交

- [ ] **Step 1: Import 链验证**

Run: `python3 -c "from niu_api.compat import tidy_context, _is_subagent_overflow, _extract_overflow_info, _write_cursor_with_lock; print('compat OK')"`
Expected: compat OK

Run: `python3 -c "from agent.runner import GenericAgentRunner; print('runner OK')"`
Expected: runner OK

Run: `python3 -c "from agent.handler import NiuHandler; print('handler OK')"`
Expected: handler OK

- [ ] **Step 2: grep 确认无残留 `_extract_cursor_id` 调用**

Run: `grep -rn "_extract_cursor_id" niu_api/ agent/ tests/ --include="*.py"`
Expected: 无结果（函数定义和测试均已删除）

- [ ] **Step 3: 提交**

```bash
git add niu_api/compat.py agent/runner.py agent/handler.py tests/test_tidy_cursor.py config/agents/entity-extractor.md config/agents/dream-evolver.md config/agents/journal-agent.md config/agents/context-manager.md
git commit -m "refactor: auto-advance subagent cursors instead of LLM-reported UUIDs

Replace cursor extraction from LLM output with programmatic cursor
advancement: success → advance to last incremental message, overflow
→ keep cursor unchanged. This eliminates LLM format non-compliance,
hallucinated UUIDs, and PROTECTED cursor stalling risks.

Changes span 3 code paths (compat sleep/force mode, runner force mode,
handler journal cursor) and 3 agent prompt files. Removes _extract_cursor_id
function and its tests."
```
