# 模式2压缩改用模式3架构：全量消息+一轮JSON方案+程序化执行

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复模式2压缩只传37条消息的根因问题，改用模式3的"一轮JSON方案+程序化执行"架构，确保子Agent能看到全量消息并从远端开始压缩。

**Architecture:** 模式2压缩改为：1) 全量消息传入子Agent（不截断远端）；2) 关闭子Agent FIFO（`context_fifo_threshold=0`）；3) 子Agent一轮write返回JSON压缩方案；4) 程序化安全执行方案（pause+drain ChatQueue + acquire chat_lock + 原子执行）；5) 恢复ChatQueue。prompt使用模式二规则（区域划分+分层压缩），但执行方式与模式三相同。

**Tech Stack:** Python (FastAPI, asyncio), SQLite (MessageStore), ChatQueue (pause/drain/resume)

---

## 根因

`_truncate_preserving_tail`（compat.py:1502-1504）把全量消息截断为只保留近端37条（`context_window * 0.4 = 80000 tokens`），远端需要压缩的消息被砍掉。子Agent连远端消息都看不到，无法执行压缩。

## 设计要点

1. **全量消息传入**：模式2不再对 `compress_msg_text` 做预截断，只在最终prompt上做一次 `_truncate_preserving_both`（保头保尾弃中间）
2. **关闭FIFO**：`context_fifo_threshold=0`，子Agent内部不再截断历史，完整保留所有传入消息
3. **一轮JSON方案**：子Agent只调用write输出压缩方案，不再多轮调用update_message/delete_messages
4. **程序化安全执行**：pause ChatQueue + 等待worker空闲 + acquire chat_lock，重新读取最新消息做安全过滤，再执行delete/update。任何步骤超时则中止，不继续执行
5. **不写游标**：模式2保持"无游标"设计不变（`_is_mode2` 时始终 `_compress_cursor=""`），避免与模式1的增量范围逻辑冲突
6. **prompt改模式二规则**：prompt内容仍用模式二的区域划分+分层压缩规则，但末尾要求一轮write输出JSON方案
7. **dream边界保护与模式三一致**：dream游标之后的消息既不允许delete也不允许update（内容替换）

## 修改文件

| 文件 | 修改内容 |
|------|----------|
| `niu_api/compat.py` | 模式2压缩流程：改截断策略、改子Agent调用方式、改prompt、改结果执行方式、不写游标 |
| `config/agents/context-manager.md` | 模式二规则补充：一轮JSON方案格式说明 |

---

### Task 1: 模式2压缩改用一轮JSON方案+程序化执行

**Files:**
- Modify: `niu_api/compat.py:1492-1684`

这是核心改动，将模式2压缩从"子Agent多轮工具调用直接改DB"改为"一轮write JSON方案+程序化安全执行"。

#### 改动 1a: 移除compress_msg_text的预截断（仅模式2）

当前第 1501-1508 行：
```python
# 限制全量范围的 token 总量，避免截断砍掉近端消息
_compress_window = int(_read_context_window_tokens() * 0.4)
if compress_msg_text and _estimate_text_tokens(compress_msg_text) > _compress_window:
    compress_msg_text = _truncate_preserving_tail(compress_msg_text, _compress_window)
    # 截断后重建 compress_msg_ids，只保留可见消息的 ID
    _visible_ids = re.findall(r'\[id:([a-f0-9-]+)\]', compress_msg_text)
    _visible_set = set(_visible_ids)
    compress_msg_ids = [mid for mid in compress_msg_ids if mid in _visible_set]
```

替换为：模式2不做预截断（全量传入），模式1保留原有逻辑：
```python
if not _is_mode2:
    # 模式一：限制全量范围的 token 总量，避免截断砍掉近端消息
    _compress_window = int(_read_context_window_tokens() * 0.4)
    if compress_msg_text and _estimate_text_tokens(compress_msg_text) > _compress_window:
        compress_msg_text = _truncate_preserving_tail(compress_msg_text, _compress_window)
        _visible_ids = re.findall(r'\[id:([a-f0-9-]+)\]', compress_msg_text)
        _visible_set = set(_visible_ids)
        compress_msg_ids = [mid for mid in compress_msg_ids if mid in _visible_set]
```

**注意**：模式2不做预截断的原因是 compress_msg_text 会被嵌入到 prompt 中，prompt 整体在改动1c中做一次截断即可。双重截断会导致不可预测的结果（第一次截断丢中间，第二次再截断丢更多中间）。

#### 改动 1b: prompt 改为一轮JSON方案

当前第 1514-1529 行构建的 `_cursor_instruction` 和 `_compress_target`，模式2时需要改为要求一轮write输出JSON方案。

在 `_is_mode2` 判断块中（第 1514 行之后），修改 `_cursor_instruction`：
```python
if _is_mode2:
    # ... 已有的 _compress_target 和 _skip_compress 逻辑不变 ...
    # 模式2改为一轮JSON方案，不要求游标报告
    _cursor_instruction = ""
else:
    _cursor_instruction = """处理完成后，在报告末尾用 JSON 格式报告：{"last_compress_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}
**必须推进游标**：即使没有需要处理的内容，也必须输出 idx 最大的消息的 UUID。"""
```

然后在 prompt 构建处（第 1550 行），模式2时使用不同的 prompt：

```python
if _is_mode2:
    compress_plan_path = os.path.expanduser("~/.niu/compress_plan_mode2.json")
    # 清理残留计划文件
    if os.path.exists(compress_plan_path):
        try:
            os.remove(compress_plan_path)
        except OSError:
            pass

    prompt = f"""系统进入睡眠状态。

当前上下文：{display_tokens} tokens（{usage_percent:.1f}%）
{_compress_target}以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并），在单元内应排除不参与压缩：
保护消息ID: {json.dumps(protected_ids)}

消息列表：
{compress_msg_text}

请按照【模式二：睡眠整理（半破坏性）】的规则处理。

CRITICAL: 你只有一轮机会完成所有压缩决策。多轮工具调用会浪费上下文，降低压缩质量。
- 禁止使用 delete_messages、update_message、get_messages 等会话管理工具。
- 禁止使用 bash、code_run、read、edit 等工具。
- 只允许使用 write 工具一次性输出压缩方案。

用 write 工具写入 {compress_plan_path}，内容为 JSON：
{{"deletes": ["要删除的消息id1", "id2", ...], "updates": [{{"message_id": "id", "content": "压缩后的摘要内容"}}], "last_compress_id": "操作范围内 idx 最大的、且仍存在的消息 id（UUID）"}}

REMINDER: 从远端（idx小的）开始压缩，近端保护消息不要动。只使用 write 工具。"""
else:
    prompt = f"""系统进入睡眠状态。

当前上下文：{display_tokens} tokens（{usage_percent:.1f}%）
{_compress_target}以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并），在单元内应排除不参与压缩：
保护消息ID: {json.dumps(protected_ids)}

消息列表：
{compress_msg_text}

请按照【{compress_mode}】的规则处理。{_cursor_instruction}"""
```

**注意**：`compress_plan_path` 使用 `compress_plan_mode2.json`（而非模式三的 `compress_plan.json`），避免两种模式互相冲突。

#### 改动 1c: 子Agent调用改为关闭FIFO + 单次截断prompt

当前第 1562-1593 行（模式2的prompt截断+子Agent调用），替换为：

```python
if _is_mode2:
    # 模式2：全量传入，关闭FIFO，一轮write输出方案
    # 对完整prompt做一次截断（保头保尾弃中间），预算 0.55
    try:
        from agent.token_calculator import TokenCalculator
        _tc = TokenCalculator.get().count_text(prompt)
    except Exception:
        _tc = len(prompt) // 2
    context_window_for_truncate = _read_context_window_tokens()
    if _tc >= context_window_for_truncate * 0.55:
        prompt = _truncate_preserving_both(prompt, int(context_window_for_truncate * 0.55))
        # 截断后重建 compress_msg_ids，只保留 LLM 可见的消息
        _visible_ids_2 = re.findall(r'\[id:([a-f0-9-]+)\]', prompt)
        _visible_set_2 = set(_visible_ids_2)
        compress_msg_ids = [mid for mid in compress_msg_ids if mid in _visible_set_2]
        # 注意：不修改 protected_ids，完整性检查需要验证所有受保护消息

    def run_context_manager_mode2():
        return call_subagent(
            agent_name="context-manager",
            task=prompt,
            llm_config=llm_config,
            mcp_client=None,
            context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
        )

    compress_result = await asyncio.to_thread(run_context_manager_mode2)
    if is_stop_requested():
        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
        clear_stop()
        return {"status": "aborted", "message": "Stopped by user"}
    logger.info(f"[Tidy] Mode-2: context-manager completed, length={len(compress_result)}")
else:
    # 模式一：原有逻辑不变
    # 截断 task 防止子Agent超限
    context_window_for_truncate = _read_context_window_tokens()
    safe_tokens = int(context_window_for_truncate * 0.6)
    truncated_prompt = _truncate_task_for_subagent(prompt, safe_tokens)

    def run_context_manager():
        return call_subagent(
            agent_name="context-manager",
            task=truncated_prompt,
            llm_config=llm_config,
            mcp_client=None,
        )

    cm_result = await asyncio.to_thread(run_context_manager)
    if is_stop_requested():
        logger.warning("[Tidy] Stop requested, aborting tidy pipeline")
        clear_stop()
        return {"status": "aborted", "message": "Stopped by user"}
    logger.info(f"[Tidy] context-manager result: {cm_result[:200]}")
```

#### 改动 1d: 程序化执行JSON方案（安全协议：pause+drain+lock+原子执行）

在子Agent返回后（模式2分支内），添加读取+安全过滤+执行逻辑。

**安全协议**：
1. `ChatQueue.pause()` — 设置暂停标志，worker不再处理新消息
2. 等待 `_processing_done` 事件 — 确保worker当前处理完成（不用drain，因为drain会清空队列丢弃用户消息）
3. `_chat_lock.acquire(timeout=60)` — 阻止新的chat请求修改DB
4. 执行安全过滤 + delete/update
5. `_chat_lock.release()` → `ChatQueue.resume()` — 恢复

任何步骤超时则中止，绝不继续执行。

```python
if _is_mode2:
    # 模式二：程序化执行JSON压缩方案
    if os.path.exists(compress_plan_path):
        try:
            plan_text = Path(compress_plan_path).read_text(encoding="utf-8")
            plan = json.loads(plan_text)
            deletes = plan.get("deletes", [])
            updates = plan.get("updates", [])
            plan_compress_id = plan.get("last_compress_id", "")

            # 类型校验
            if not isinstance(deletes, list):
                deletes = []
            if not isinstance(updates, list):
                updates = []
            else:
                updates = [u for u in updates if isinstance(u, dict)]

            # 安全协议：pause + 等待worker空闲 + acquire chat_lock
            from niu_api.chat_queue import get_chat_queue
            _q = get_chat_queue()
            _q.pause()

            # 等待worker当前处理完成（不用drain，drain会清空队列丢弃用户消息）
            if _q._processing:
                try:
                    await asyncio.wait_for(_q._processing_done.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    logger.warning("[Tidy] Mode-2: ChatQueue processing timeout, aborting execution")
                    _q.resume()
                    try:
                        os.remove(compress_plan_path)
                    except OSError:
                        pass
                    # 跳到 finally 清理
                    raise RuntimeError("ChatQueue processing timeout")

            # acquire chat_lock：阻止其他chat请求修改DB
            from niu_api.chat import _chat_lock
            _chat_lock_acquired = False
            try:
                await asyncio.wait_for(_chat_lock.acquire(), timeout=60.0)
                _chat_lock_acquired = True
            except asyncio.TimeoutError:
                logger.warning("[Tidy] Mode-2: chat_lock 60s timeout, aborting execution")

            if not _chat_lock_acquired:
                _q.resume()
                try:
                    os.remove(compress_plan_path)
                except OSError:
                    pass
                raise RuntimeError("chat_lock timeout")

            # === 在 chat_lock 保护下执行 ===
            try:
                # 重新获取最新消息快照（子Agent执行期间可能有新消息）
                fresh_messages = await store.get_messages()
                existing_ids = {getattr(m, "id", "") for m in fresh_messages}

                # ID有效性校验
                valid_deletes = [mid for mid in deletes if mid in existing_ids]
                valid_deletes = list(dict.fromkeys(valid_deletes))  # 去重
                valid_updates = [u for u in updates if u.get("message_id") and u["message_id"] in existing_ids]

                # 游标保护：禁止删除/压缩游标指向的消息
                cursor_ids_set = {cid for cid in [new_entity_id, new_dream_id] if cid}
                valid_deletes = [mid for mid in valid_deletes if mid not in cursor_ids_set]
                valid_updates = [u for u in valid_updates if u.get("message_id", "") not in cursor_ids_set]

                # dream安全边界：dream游标之后的消息不得删除，也不得update（内容替换会丢失未提取知识）
                if new_dream_id:
                    dream_boundary_idx = -1
                    for i, m in enumerate(fresh_messages):
                        if (getattr(m, "id", "") or "") == new_dream_id:
                            dream_boundary_idx = i
                            break
                    if dream_boundary_idx >= 0:
                        post_dream_ids = {getattr(m, "id", "") for m in fresh_messages[dream_boundary_idx + 1:]}
                        unsafe_deletes = [mid for mid in valid_deletes if mid in post_dream_ids]
                        unsafe_updates = [u for u in valid_updates if u.get("message_id", "") in post_dream_ids]
                        if unsafe_deletes:
                            logger.warning(f"[Tidy] Mode-2: Protecting {len(unsafe_deletes)} post-dream messages from deletion")
                            valid_deletes = [mid for mid in valid_deletes if mid not in post_dream_ids]
                        if unsafe_updates:
                            logger.warning(f"[Tidy] Mode-2: Protecting {len(unsafe_updates)} post-dream messages from content replacement")
                            valid_updates = [u for u in valid_updates if u.get("message_id", "") not in post_dream_ids]

                # protected保护：最近N条user/assistant消息不可动
                protect_recent_count = _read_protect_recent_count()
                if protect_recent_count > 0:
                    _pids = []
                    for m in reversed(fresh_messages):
                        if getattr(m, "role", "") in ("user", "assistant"):
                            _pids.append(getattr(m, "id", ""))
                        if len(_pids) >= protect_recent_count:
                            break
                    protected_set = set(_pids)
                    valid_deletes = [mid for mid in valid_deletes if mid not in protected_set]
                    valid_updates = [u for u in valid_updates if u.get("message_id", "") not in protected_set]

                # delete/update重叠处理：同一ID同时出现在deletes和updates中时，保留update
                update_ids = {u.get("message_id", "") for u in valid_updates}
                overlap_ids = update_ids & set(valid_deletes)
                if overlap_ids:
                    logger.warning(f"[Tidy] Mode-2: Removing {len(overlap_ids)} IDs from deletes that also appear in updates")
                    valid_deletes = [mid for mid in valid_deletes if mid not in overlap_ids]

                # 执行删除
                if valid_deletes:
                    del_result = await store.delete_messages_by_ids(valid_deletes)
                    logger.info(f"[Tidy] Mode-2: Deleted {del_result.get('deleted_count', 0)} messages, freed {del_result.get('freed_tokens', 0)} tokens")

                # 执行更新
                for upd in valid_updates:
                    mid = upd.get("message_id", "")
                    content = upd.get("content", "")
                    if mid and content:
                        ok = await store.update_message(message_id=mid, content=content)
                        if ok:
                            logger.info(f"[Tidy] Mode-2: Updated message {mid}")
                        else:
                            logger.warning(f"[Tidy] Mode-2: Failed to update message {mid}")

                logger.info(f"[Tidy] Mode-2: Compression plan executed: {len(valid_deletes)} deletes, {len(valid_updates)} updates")
            finally:
                if _chat_lock_acquired:
                    _chat_lock.release()
                _q.resume()

        except json.JSONDecodeError as e:
            logger.error(f"[Tidy] Mode-2: Failed to parse compress plan JSON: {e}")
        except RuntimeError:
            pass  # 已在上方处理（ChatQueue timeout / chat_lock timeout）
        except Exception as e:
            logger.error(f"[Tidy] Mode-2: Failed to execute compress plan: {e}")
        finally:
            if os.path.exists(compress_plan_path):
                try:
                    os.remove(compress_plan_path)
                except OSError:
                    pass
    else:
        logger.warning("[Tidy] Mode-2: No compress plan file found, sub-agent may not have used write")

    # 模式二不写游标：保持"无游标"设计，避免与模式一增量逻辑冲突
    # 每次模式二触发时始终全量处理（_compress_cursor=""）
    logger.info("[Tidy] Mode-2: Compression complete (no cursor update, mode-2 is always full-range)")
```

#### 改动 1e: 删除旧的模式2多轮工具调用代码 + 游标处理代码

当前第 1602-1684 行的代码结构需要重构为：
```python
if _is_mode2:
    # 改动1c: 一轮write子Agent调用（关闭FIFO）
    # 改动1d: 程序化执行JSON方案
    # 不写游标
else:
    # 模式一: 原有逻辑完整保留
    # 包括游标提取（_extract_cursor_id）、溢出处理、
    # 完整性校验（compress_integrity_ok）、游标写入等
```

具体来说：
- 模式2分支中删除 `_extract_cursor_id` 的游标提取逻辑（一轮JSON方案不需要从文本中提取游标）
- 模式2分支中删除 `compress_integrity_ok` 的完整性校验（程序化安全过滤已替代）
- 模式2分支中不写压缩游标（保持 `_is_mode2` 时始终全量处理的设计）
- 模式一的游标提取、完整性校验、游标写入逻辑完整保留

#### 改动 1f: 独立的计划文件路径

`compress_plan_path` 使用 `~/.niu/compress_plan_mode2.json`（改动1b中已设置），与模式三的 `~/.niu/compress_plan.json` 互不干扰。

- [ ] **Step 1: 读取 compat.py 第1492-1684行，确认当前代码结构**

- [ ] **Step 2: 移除compress_msg_text预截断（改动1a）**

将第1501-1508行的截断逻辑包裹在 `if not _is_mode2:` 条件中，模式2不截断。

- [ ] **Step 3: 修改prompt构建（改动1b）**

在第1550行附近，模式2时构建一轮JSON方案prompt（含compress_plan_mode2.json路径），模式1保留原有prompt。

- [ ] **Step 4: 修改子Agent调用+结果执行（改动1c+1d+1e）**

将第1562-1684行重构为 `if _is_mode2: ... else: ...` 分支：
- 模式2：关闭FIFO调用子Agent + 安全协议执行JSON方案（pause+drain+lock+原子执行）+ 不写游标
- 模式1：保留原有逻辑完整不变

- [ ] **Step 5: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 6: 提交**

```bash
git add niu_api/compat.py
git commit -m "fix: mode-2 compression uses one-shot JSON plan + programmatic execution instead of multi-turn tool calls"
```

---

### Task 2: context-manager.md 补充模式二一轮JSON方案说明

**Files:**
- Modify: `config/agents/context-manager.md`

在模式二规则末尾（"释放不足时的处理"之后），补充一轮JSON方案说明：

```markdown
**模式二执行方式变更**：

当程序指明使用一轮JSON方案时（prompt 中包含 "CRITICAL: 你只有一轮机会"），你必须：
1. 一次性分析所有消息，做出全部压缩决策
2. 用 write 工具输出 JSON 压缩方案到指定路径
3. 禁止调用 delete_messages、update_message、get_messages 等会话管理工具
4. 禁止调用 bash、code_run 等其他工具

JSON 方案格式与模式三相同：
```json
{
  "deletes": ["要删除的消息id1", "id2", ...],
  "updates": [{"message_id": "id", "content": "压缩后的摘要内容"}, ...],
  "last_compress_id": "操作范围内 idx 最大的、且仍存在的消息 id（UUID）"
}
```

**压缩规则仍按模式二的区域划分+分层压缩执行**——远端区多压、中端区适度、近端区轻度、保护区不动。区别只是执行方式从多轮工具调用变为一轮write输出方案。
```

- [ ] **Step 1: 在 context-manager.md 模式二规则末尾添加一轮JSON方案说明**

- [ ] **Step 2: 提交**

```bash
git add config/agents/context-manager.md
git commit -m "docs: add one-shot JSON plan instruction to context-manager mode-2 rules"
```

---

### Task 3: 移除旧模式2的多轮工具调用后置校验代码

**Files:**
- Modify: `niu_api/compat.py` — 确认模式2不再需要旧的完整性校验

模式2改为一轮JSON方案后，完整性校验由程序化安全过滤替代（改动1d中的 ID校验+游标保护+dream边界+protected保护+重叠处理）。旧的后置校验代码（`compress_integrity_ok`、`protected_originals` 对比等）只在模式1中需要，需要确认模式2路径中不再引用。

- [ ] **Step 1: 搜索 compress_integrity_ok 在模式2路径中的使用，确认已移除**

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 3: 提交**

```bash
git add niu_api/compat.py
git commit -m "cleanup: remove stale mode-2 post-compression validation code replaced by programmatic filtering"
```

---

## 审查修复记录

### 第一轮（code-reviewer审查）

| # | 严重度 | 问题 | 修复方案 |
|---|--------|------|----------|
| C1 | Critical | ChatQueue.pause()存在竞态窗口——已出队但未暂停的消息仍会被处理 | pause后等待_processing_done事件（不用drain，drain会清空队列丢弃用户消息），确保worker空闲 |
| C2 | Critical | chat_lock获取顺序与ChatQueue._process_single死锁 | 等待_processing_done后再acquire，且超时后中止而非继续 |
| C3 | Critical | chat_lock超时后继续执行不安全 | 超时则中止，不执行delete/update |
| C4 | Important | 模式3程序化执行无并发保护 | 模式3从_check_overflow调用（在_process_single内部，此时ChatQueue worker忙碌），实际安全；但模式2从_tidy_context_impl直接调用，不安全，需要自己的保护 |
| C5 | Critical | 双重截断（1a截断compress_msg_text + 1c截断prompt）导致不可预测结果 | 移除1a预截断，仅在1c对完整prompt做一次截断 |
| C6 | Critical | 游标写入破坏模式2"无游标"设计，后续模式1会用该游标做增量范围 | 模式2不写游标，保持_compress_cursor=""始终全量 |
| A7 | Important | _truncate_preserving_both回退路径可能截断指令 | 确保prompt包含"消息列表："分割点；回退路径已保留"保护消息ID:"行 |
| A8 | Important | last_compress_id可能指向被截断掉的近端消息 | 模式2不写游标，此问题不存在 |
| A9 | Important | compress_plan_path全局固定路径，模式2和模式3冲突 | 使用独立路径compress_plan_mode2.json |
| A10 | Important | 模式2 dream边界保护只禁止delete，模式三禁止delete和update | 与模式三一致：dream之后的消息既不允许delete也不允许update |
| A11 | Important | 子Agent执行期间ChatQueue可能处理新消息，fresh_messages包含新消息 | 安全协议pause+等待_processing_done+lock确保执行期间无新消息写入；fresh_messages是原子快照 |

---

## 验证

1. 启动程序，正常使用直到上下文超过 50%
2. 触发睡眠模式（POST /api/tidy_context mode=sleep）
3. 检查日志：
   - 模式2 prompt 中消息数量应远大于37条（取决于双向截断保留了多少）
   - 子Agent调用带 `context_fifo_threshold=0`
   - 子Agent输出应包含 write 工具调用
   - 程序化执行前应有 `ChatQueue.pause()` + `drain()` + `chat_lock.acquire()` 日志
   - 压缩后 token 量应接近 targetThreshold 目标
4. 检查压缩后消息列表：
   - 远端消息应被压缩为摘要
   - 近端10条 user/assistant 消息应完整保留
   - 新写入的消息（如果压缩期间有新消息）不应被误删
5. 模式1压缩应不受影响（仍用多轮工具调用方式）
6. 压缩游标文件不应被模式2更新（模式2始终全量处理）
