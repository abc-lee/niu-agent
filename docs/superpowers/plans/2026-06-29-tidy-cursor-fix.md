# 游标推进 bug 修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复模式一压缩后游标盲取增量范围末尾消息、导致被删除后回退重复压缩的 bug。

**Architecture:** 将 `new_compress_id = compress_msg_ids[-1]` 改为：压缩后重新读取 DB，在 `compress_msg_ids` 范围内取仍存在于 DB 中的最后一条消息 ID。

**Tech Stack:** Python 3.11+, aiosqlite

---

## 问题分析

### Bug 位置
`niu_api/compat.py:2014`

### 当前代码
```python
new_compress_id = compress_msg_ids[-1] if compress_msg_ids else last_compress_id
```

### 问题
`compress_msg_ids` 是增量范围内所有消息 ID 列表。`[-1]` 盲取最后一条。但 context-manager 可能删除该消息（如本次日志中 `fca54dd9` 被删除）。

### 现有保护
第2018-2025行有校验+回退：如果 `new_compress_id` 不在 DB 中，回退到 `last_compress_id`。这避免了崩溃，但回退导致**下次压缩重复处理已压缩范围**，浪费 LLM 调用。

### 修复方案
不盲取末尾，改为在 `compress_msg_ids` 中找到压缩后仍存在于 DB 中的最后一条：

```python
# 压缩后重新读取 DB，在增量范围内取仍存在的最后一条
fresh_msgs = await store.get_messages()
fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
surviving = [mid for mid in compress_msg_ids if mid in fresh_ids]
new_compress_id = surviving[-1] if surviving else last_compress_id
logger.info(f"[Tidy] Compress cursor auto-advanced to: {new_compress_id}")
```

这样游标指向压缩后仍存在的最后一条消息，不会被回退。后续的第2018-2025行校验变为冗余（但保留作为防御）。

---

### Task 1: 修复游标推进逻辑

**Files:**
- Modify: `niu_api/compat.py:2008-2025`

- [ ] **Step 1: 读取当前代码确认**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && sed -n '2008,2026p' niu_api/compat.py`

确认当前代码：
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
                        # PROTECTED 消息已从 compress_msg_ids 中排除，游标不可能指向 PROTECTED 消息
```

- [ ] **Step 2: 修改游标推进逻辑**

用 Edit 工具，old_string：
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
                        # PROTECTED 消息已从 compress_msg_ids 中排除，游标不可能指向 PROTECTED 消息
```

new_string：
```python
                    # 游标自动推进：成功→推进到范围内仍存在的最后一条，overflow→不动
                    if _is_subagent_overflow(cm_result):
                        overflow_info = _extract_overflow_info(cm_result)
                        logger.warning(f"[Tidy] context-manager overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动
                    else:
                        # 不盲取 compress_msg_ids[-1]（可能被 context-manager 删除），
                        # 而是重新读取 DB，取范围内仍存在的最后一条
                        fresh_msgs = await store.get_messages()
                        fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        surviving = [mid for mid in compress_msg_ids if mid in fresh_ids]
                        new_compress_id = surviving[-1] if surviving else last_compress_id
                        logger.info(f"[Tidy] Compress cursor auto-advanced to: {new_compress_id}")

                    # 校验游标（防御性：确保游标指向存在的消息）
                    if new_compress_id:
                        if 'fresh_ids' not in dir():
                            fresh_msgs = await store.get_messages()
                            fresh_ids = {getattr(m, "id", "") for m in fresh_msgs}
                        if new_compress_id not in fresh_ids:
                            logger.warning(f"[Tidy] Compress cursor {new_compress_id} not in DB, reverting to {last_compress_id}")
                            new_compress_id = last_compress_id
                            if new_compress_id and new_compress_id not in fresh_ids:
                                new_compress_id = ""
                        # PROTECTED 消息已从 compress_msg_ids 中排除，游标不可能指向 PROTECTED 消息
```

说明：
- 推进逻辑改为重新读取 DB，在 `compress_msg_ids` 中找仍存在的最后一条
- 校验逻辑保留作为防御（理论上不再触发回退，但保留兜底）
- `'fresh_ids' not in dir()` 避免重复读取 DB（overflow 路径没读 fresh_ids）

- [ ] **Step 3: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "from niu_api import compat; print('OK')"`

Expected: `OK`

- [ ] **Step 4: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py
git commit -m "fix(tidy): compress cursor picks last surviving message instead of blind tail

游标盲取 compress_msg_ids[-1] 可能指向被 context-manager 删除的消息，
触发回退导致下次重复压缩。改为重新读取 DB，取范围内仍存在的最后一条。"
```

---

## 自审检查

### 1. Spec 覆盖
- 游标盲取 bug → Task 1 Step 2 ✅
- 校验逻辑保留 → Task 1 Step 2 ✅

### 2. Placeholder 扫描
无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性
- `compress_msg_ids` 是 list[str]，`surviving` 是 list[str]，`surviving[-1]` 是 str
- `fresh_ids` 是 set[str]
- `new_compress_id` 是 str
- 类型一致

### 4. 边界条件
- `compress_msg_ids` 为空 → `surviving` 为空 → `new_compress_id = last_compress_id`（与原逻辑一致）
- 所有消息都被删除 → `surviving` 为空 → 同上
- overflow 路径 → 不进入 else 分支，`new_compress_id` 未赋值 → 后续校验 `if new_compress_id:` 为 False，跳过（与原逻辑一致，overflow 时游标不动）

### 5. 风险评估
- 改动范围：1处游标推进逻辑，~10行
- 校验逻辑保留为防御，不破坏现有保护
- overflow 路径不受影响
- 无新增依赖
