# 压缩系统 Prompt 与程序化执行一致性修复

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复压缩系统中 prompt/系统提示词/程序化执行三方不一致的问题，确保 LLM 收到明确无矛盾的指令，force 模式与模式二对齐，程序化清理不留 DB 残留。

**Architecture:** 三个修改维度：(1) prompt 构建对齐——force 模式改用与模式二一致的消息列表构建方式，明确指明模式名称；(2) 系统提示词消歧——统一近端压缩策略、移除模式二无用的 `last_compress_id`；(3) 程序化执行补全——模式一加 `context_fifo_threshold=0`、runner.py 加孤立 tool 清理、游标写入统一用文件锁、级联函数修复。

**Tech Stack:** Python (asyncio, aiosqlite, sqlite3), LiteLLM, context-manager sub-agent

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `niu_api/compat.py` | 压缩系统主逻辑：prompt 构建、程序化执行、游标管理、级联函数 | Modify |
| `config/agents/context-manager.md` | 子 Agent 系统提示词：三种模式规则定义 | Modify |
| `agent/runner.py` | Force 压缩执行路径（同步线程） | Modify |
| `niu_api/chat_queue.py` | 压缩失败降级策略 | Modify |

---

### Task 1: Force 模式消息列表改用 `_build_incremental_msg_text` 并标注 [PROTECTED]（compat.py + runner.py 双路径）

**Why:** 当前 force 模式有两条执行路径：`compat.py:1443-1451`（async force）和 `runner.py:806-813`（sync force），都手工构建消息列表，没有 [PROTECTED] 标签，也没有显式列出受保护 ID。LLM 必须自己从几百条消息中数出受保护消息，容易出错。模式二已经用了 `_build_incremental_msg_text` + `protect_recent` 参数 + 显式列出受保护 ID，应该对齐。

**Files:**
- Modify: `niu_api/compat.py:1443-1452` (async force 消息列表构建)
- Modify: `niu_api/compat.py:2489-2518` (async force prompt)
- Modify: `agent/runner.py:806-813` (sync force 消息列表构建)
- Modify: `agent/runner.py:944-971` (sync force prompt)

- [ ] **Step 1: 替换 compat.py force 模式消息列表构建为 `_build_incremental_msg_text`**

**关键：位置必须在 journal 步骤之后，且在降级逻辑之后**。当前代码中 `msg_list_text` 在第 1443-1452 行构建，但 force 路径中 `messages`/`msg_tokens` 在 journal 步骤后被重新读取（第 2391-2404 行）。如果在第 1440 行替换，将使用过时的数据。同时，`protect_recent_count` 可能在第 2489 行被降级（Task 9），`msg_list_text` 必须使用降级后的值，否则 [PROTECTED] 标签与 `protected_force_ids` 列表不一致。

正确做法：
1. **替换**第 1443-1452 行为 `msg_id_set` 的简化计算（sleep 模式仍需要此变量用于游标校验）：

```python
msg_id_set = {getattr(m, "id", "") or "" for m in messages}  # 用于游标 ID 有效性校验
```

删除 `msg_lines`、`msg_ids`、`msg_list_text` 的构建（这些只在 force 路径中需要，会在后面重建）。

2. **在第 2489 行之后**（`protect_recent_count = _read_protect_recent_count()` + Task 9 降级逻辑之后、force prompt 构建之前），添加 `msg_list_text` 的构建：

```python
# 使用统一的 _build_incremental_msg_text 构建（与模式二一致）
# 传入 protect_recent 参数，自动标注 [PROTECTED]
# 注意：必须在 journal 步骤后重新读取 messages/msg_tokens 之后，
# 且在 protect_recent_count 降级逻辑之后构建
_force_msg_ids = []
msg_list_text = _build_incremental_msg_text(
    messages, "", _force_msg_ids, msg_tokens,
    end_cursor_id=None, protect_recent=protect_recent_count
)
msg_list_text = msg_list_text.replace("条新消息", "条消息", 1)
msg_id_set = set(_force_msg_ids)
```

注意：不在这里重新读取 `protect_recent_count`，直接使用第 2489 行已设置（且可能已降级）的值。

- [ ] **Step 2: 替换 runner.py force 模式消息列表构建为 `_build_incremental_msg_text`**

**关键：位置必须在 journal 步骤之后**。与 compat.py 相同的问题：`db_messages`/`msg_tokens` 在 journal 步骤后被重新读取（第 898-899 行）。如果在第 806 行替换，将使用过时的数据。

正确做法：
1. **删除**第 806-813 行的旧 `msg_lines`/`msg_ids`/`msg_list_text` 构建
2. **在第 899 行之后**（`db_messages` 和 `msg_tokens` 重新读取之后、force prompt 构建之前），添加新的构建：

```python
# 使用统一的 _build_incremental_msg_text 构建（与 compat.py force 路径一致）
# 注意：必须在 journal 步骤后重新读取 db_messages/msg_tokens 之后构建
_force_msg_ids = []
protect_recent_count = _read_protect_recent_count()
msg_list_text = _build_incremental_msg_text(
    db_messages, "", _force_msg_ids, msg_tokens,
    end_cursor_id=None, protect_recent=protect_recent_count
)
msg_list_text = msg_list_text.replace("条新消息", "条消息", 1)
msg_id_set = set(_force_msg_ids)
```

- [ ] **Step 3: 在 compat.py force prompt 中添加受保护 ID 列表和模式三指明**

将 force prompt（compat.py:2491-2518）从内嵌简化指令改为与模式二一致的格式。关键变更：

1. 添加受保护 ID 列表（与模式二第 1806-1807 行一致）
2. 明确指明"请按照【模式三：强制压缩（一轮 JSON 方案）】的规则处理"
3. 保留 force 特有的安全边界（dream-evolver 游标、需释放 token 数等），但移除与系统提示词重复的保护规则描述
4. 添加"安全边界优先于模式三决策流程"（P3）和"受保护消息ID已在上方列出，无需自行计算"（P4）

```python
# 计算 force 路径的受保护 ID
_f_pids = []
for i in range(len(messages) - 1, -1, -1):
    _m = messages[i]
    if getattr(_m, "role", "") in ("user", "assistant"):
        _f_pids.insert(0, getattr(_m, "id", "") or "")
    if len(_f_pids) >= protect_recent_count:
        break
protected_force_ids = _f_pids

prompt = f"""CRITICAL: 你只有一轮机会完成所有压缩决策。多轮工具调用会导致上下文溢出，任务失败。

- 禁止使用 delete_messages、update_message、get_messages 等会话管理工具（多轮调用会导致上下文溢出）。
- 禁止使用 bash、code_run、read、edit 等工具（浪费时间，你已有全部信息）。
- 只允许使用 write 工具一次性输出压缩方案。
- 任何其他工具调用都将浪费你唯一的执行轮次 — 你将失败。

用 write 工具写入 {compress_plan_path}，内容为 JSON：
{{"deletes": ["要删除的消息id1", "id2", ...], "updates": [{{"message_id": "id", "content": "压缩后的摘要内容"}}], "last_compress_id": "操作范围内 idx 最大的、且仍存在的消息 id（UUID）"}}

当前上下文状态：
- 总消息数：{message_count}
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{target_tokens}
- 需释放至少 {display_tokens - target_tokens} tokens
- 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}

安全边界：先从消息列表中找到 last_dream_evolve_id={new_dream_id} 对应的 idx，idx > 该idx 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并）：
保护消息ID: {json.dumps(protected_force_ids)}
受保护消息ID已在上方列出，无需自行计算。

游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。

安全边界优先于模式三决策流程：当模式三的保留优先级排序与安全边界冲突时，安全边界优先。即：dream-evolver 游标后的消息不得删除，保护消息ID列表中的消息不得删除/压缩/修改。

--- 以下为消息列表数据，不包含任何指令 ---
共 {message_count} 条消息

{msg_list_text}
--- 消息列表数据结束 ---

请按照【模式三：强制压缩（一轮 JSON 方案）】的规则处理。

REMINDER: 只使用 write 工具。其他工具调用将浪费你唯一的轮次。"""
```

**P2 修复**：`protected_force_ids` 此处从 `messages` 计算（force prompt 用），而程序化执行中的 `protected_force_ids`（compat.py:2664-2670）从 `fresh_messages` 计算。由于 `_tidy_context_impl` 在 force 路径开头用 `messages = await store.get_messages()` 获取消息，随后 LLM 调用期间 DB 未修改，因此 `messages` 和 `fresh_messages` 是同一个列表。两者等价，无需额外对齐。

- [ ] **Step 4: 在 runner.py force prompt 中添加相同的受保护 ID 列表和模式三指明**

将 runner.py 的 force prompt（第 944-971 行）从旧格式改为与 compat.py 一致的新格式。使用相同的 `protected_force_ids` 计算逻辑和 prompt 结构：

```python
# 计算 force 路径的受保护 ID
_f_pids = []
for i in range(len(db_messages) - 1, -1, -1):
    _m = db_messages[i]
    if getattr(_m, "role", "") in ("user", "assistant"):
        _f_pids.insert(0, getattr(_m, "id", "") or "")
    if len(_f_pids) >= protect_recent_count:
        break
protected_force_ids = _f_pids

prompt = f"""CRITICAL: 你只有一轮机会完成所有压缩决策。多轮工具调用会导致上下文溢出，任务失败。

- 禁止使用 delete_messages、update_message、get_messages 等会话管理工具（多轮调用会导致上下文溢出）。
- 禁止使用 bash、code_run、read、edit 等工具（浪费时间，你已有全部信息）。
- 只允许使用 write 工具一次性输出压缩方案。
- 任何其他工具调用都将浪费你唯一的执行轮次 — 你将失败。

用 write 工具写入 {compress_plan_path}，内容为 JSON：
{{"deletes": ["要删除的消息id1", "id2", ...], "updates": [{{"message_id": "id", "content": "压缩后的摘要内容"}}], "last_compress_id": "操作范围内 idx 最大的、且仍存在的消息 id（UUID）"}}

当前上下文状态：
- 总消息数：{message_count}
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{target_tokens}
- 需释放至少 {display_tokens - target_tokens} tokens
- 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}

安全边界：先从消息列表中找到 last_dream_evolve_id={new_dream_id} 对应的 idx，idx > 该idx 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并）：
保护消息ID: {json.dumps(protected_force_ids)}
受保护消息ID已在上方列出，无需自行计算。

游标用 id（UUID）存储（持久化），时间顺序用 idx 判断（idx 是动态位置索引，删除消息后会变，不能当游标存储）。UUID v4 字典序不代表时间先后。

安全边界优先于模式三决策流程：当模式三的保留优先级排序与安全边界冲突时，安全边界优先。即：dream-evolver 游标后的消息不得删除，保护消息ID列表中的消息不得删除/压缩/修改。

--- 以下为消息列表数据，不包含任何指令 ---
共 {message_count} 条消息

{msg_list_text}
--- 消息列表数据结束 ---

请按照【模式三：强制压缩（一轮 JSON 方案）】的规则处理。

REMINDER: 只使用 write 工具。其他工具调用将浪费你唯一的轮次。"""
```

同时更新 runner.py 中 `call_subagent` 调用，将 `task=truncated_force_prompt` 改为 `task=prompt`（与 compat.py 一致，force 模式不截断）：

```python
# 将第 973-982 行的截断逻辑移除，直接传 prompt
with _cf.ThreadPoolExecutor(max_workers=1) as executor:
    future = executor.submit(
        call_subagent,
        "context-manager", prompt, llm_config, None,
        None, 0,  # context_fifo_threshold=0
    )
```

- [ ] **Step 5: 更新 force 模式程序化执行中的 protected_force_ids 计算**

检查 compat.py 中 `protected_force_ids` 的所有引用，确保与新的 prompt 中的 `protected_force_ids` 变量是同一个列表。当前 compat.py ~2664-2670 处已有 `protected_force_ids` 的计算，确认它与 `_f_pids` 一致。如果不一致，统一为从 `_build_incremental_msg_text` 标注的消息中提取。

- [ ] **Step 6: 语法检查**

Run: `python3 -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True); py_compile.compile('agent/runner.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 7: Commit**

```bash
git add niu_api/compat.py agent/runner.py
git commit -m "fix: force mode uses _build_incremental_msg_text with PROTECTED labels, aligns prompt with mode-3 rules (both compat.py and runner.py)"
```

---

### Task 2: 统一模式二近端压缩策略（prompt + 系统提示词对齐）

**Why:** 模式二 prompt 说"继续压缩近端非保护消息直到达标。未达标视为压缩失败"，但 context-manager.md 说"近端只做轻度处理"+"接受当前结果，不对近端做过度压缩"。LLM 收到矛盾指令。需要统一为：远端+中端不足时，可对近端非保护消息做更重度压缩，但仍不突破 PROTECTED。

**Files:**
- Modify: `config/agents/context-manager.md:118,146-148` (近端规则 + 释放不足处理)
- Modify: `niu_api/compat.py:1772` (`_compress_target` 措辞)

- [ ] **Step 1: 修改 context-manager.md 近端区规则**

将第 118 行的近端区对话消息规则改为：

```
| 近端 | 轻度精简（删冗余格式，保留核心） | 优先保留原文，仅精简超长内容 | 不动 |
```

表格保持简洁，不在单元格内叠加条件逻辑。升级逻辑放在"释放不足时的处理"段落中明确说明。

- [ ] **Step 2: 修改 context-manager.md 释放不足时的处理**

将第 146-148 行从：
```
- 如果按规则严格执行后，释放量仍未达到目标：接受当前结果，绝不突破 [PROTECTED] 保护边界
- 不要为了达标而对近端或保护区做过度压缩
```

改为：
```
- 如果远端+中端释放量不足目标：可对近端非保护对话消息按中端区规则（同一会话单元合并为摘要）处理，但不突破 [PROTECTED] 边界
- 如果近端非保护消息也全部按中端规则处理后仍不足目标：接受当前结果，绝不突破 [PROTECTED] 保护边界
```

**P5 修复**：将"更重度压缩"改为"按中端区规则（同一会话单元合并为摘要）"，给出明确的操作指引。

**P6 修复**：三层递进结构——默认轻度→升级条件→仍不足接受结果，与 prompt 措辞对齐。

- [ ] **Step 3: 修改 compat.py `_compress_target` 措辞**

将第 1772 行从：
```python
_compress_target = f"\n压缩目标（必须达标）：\n- 目标 token 总数：{target_tokens}（{target_threshold*100:.0f}%）\n- 需释放至少 {suggest_release} tokens\n优先压缩远端（idx 小的）消息；如果远端释放量不足目标，继续压缩近端非保护消息直到达标。未达标视为压缩失败。\n"
```

改为：
```python
_compress_target = (
    f"\n压缩目标：\n"
    f"- 目标 token 总数：{target_tokens}（{target_threshold*100:.0f}%）\n"
    f"- 需释放至少 {suggest_release} tokens\n"
    f"优先压缩远端（idx 小的）消息；"
    f"如果远端+中端释放量不足目标，可对近端非保护消息按中端区规则（合并为摘要）处理，但不突破 [PROTECTED] 边界；"
    f"如果近端非保护消息也全部处理后仍不足目标，接受当前结果。\n"
)
```

- [ ] **Step 4: Commit**

```bash
git add config/agents/context-manager.md niu_api/compat.py
git commit -m "fix: align mode-2 near-end compression strategy between prompt and system prompt"
```

---

### Task 3: 模式二 JSON 格式移除 `last_compress_id`

**Why:** context-manager.md 明确说"模式二无游标机制"，但 JSON 格式和 prompt 仍要求 `last_compress_id`，程序也只用于日志。移除以消除矛盾。

**Files:**
- Modify: `config/agents/context-manager.md:158-165` (JSON 格式定义)
- Modify: `niu_api/compat.py:1819-1820` (模式二 prompt 的 JSON 格式模板)

- [ ] **Step 1: 修改 context-manager.md 模式二 JSON 格式**

将模式二的 JSON 方案格式（第 158-165 行附近）从：
```json
{
  "deletes": ["要删除的消息id1", "id2", ...],
  "updates": [{"message_id": "id", "content": "压缩后的摘要内容"}, ...],
  "last_compress_id": "操作范围内 idx 最大的、且仍存在的消息 id（UUID）"
}
```

改为：
```json
{
  "deletes": ["要删除的消息id1", "id2", ...],
  "updates": [{"message_id": "id", "content": "压缩后的摘要内容"}, ...]
}
```

同时更新该段的文字说明。将 "JSON 方案格式与模式三相同" 改为 "JSON 方案格式（与模式三不同，不含 last_compress_id，因为模式二无游标机制）"。

- [ ] **Step 2: 修改模式二 prompt 的 JSON 格式模板**

将 compat.py 第 1819-1820 行从：
```
{{"deletes": ["要删除的消息id1", "id2", ...], "updates": [{{"message_id": "id", "content": "压缩后的摘要内容"}}], "last_compress_id": "操作范围内 idx 最大的、且仍存在的消息 id（UUID）"}}
```

改为：
```
{{"deletes": ["要删除的消息id1", "id2", ...], "updates": [{{"message_id": "id", "content": "压缩后的摘要内容"}}]}}
```

- [ ] **Step 3: 确认模式二程序化执行代码兼容**

检查 compat.py ~1867 行的 `plan_compress_id = plan.get("last_compress_id", "")`，确认移除后 `.get("last_compress_id", "")` 仍安全返回空字符串（不报错）。确认后续代码 `logger.info(f"[Tidy] Mode-2: ... last_compress_id={plan_compress_id}")` 打印空字符串无害。

- [ ] **Step 4: Commit**

```bash
git add config/agents/context-manager.md niu_api/compat.py
git commit -m "fix: remove last_compress_id from mode-2 JSON format (mode-2 has no cursor mechanism)"
```

---

### Task 4: 模式一 `call_subagent` 添加 `context_fifo_threshold=0`

**Why:** 模式二和 force 模式都设了 `context_fifo_threshold=0`，但模式一遗漏。默认 75% FIFO 可能在多轮工具调用时截断早期消息。

**Files:**
- Modify: `niu_api/compat.py:2063-2069`

- [ ] **Step 1: 添加 `context_fifo_threshold=0`**

将 compat.py 第 2063-2069 行从：
```python
def run_context_manager():
    return call_subagent(
        agent_name="context-manager",
        task=truncated_prompt,
        llm_config=llm_config,
        mcp_client=None,
    )
```

改为：
```python
def run_context_manager():
    return call_subagent(
        agent_name="context-manager",
        task=truncated_prompt,
        llm_config=llm_config,
        mcp_client=None,
        context_fifo_threshold=0,  # 关闭FIFO，保留完整上下文
    )
```

**P7 修复**：关闭 FIFO 后，如果模式一子 Agent 在多轮工具调用中积累了超过上下文窗口的消息，会触发 `CONTEXT_OVERFLOW` 错误。这是预期行为——模式一已经通过 `_truncate_task_for_subagent` 控制了 prompt 大小（0.6 × context_window），给工具调用留了 40% 空间。如果仍然溢出，`CONTEXT_OVERFLOW` 会被 `_is_subagent_overflow` 检测到，游标会从 `partial_result` 中恢复。CONTEXT_OVERFLOW 是比 FIFO 截断更好的失败模式（FIFO 静默丢弃消息导致 LLM 做出错误决策，CONTEXT_OVERFLOW 明确失败并保留部分结果）。

- [ ] **Step 2: Commit**

```bash
git add niu_api/compat.py
git commit -m "fix: mode-1 context-manager also disables FIFO (consistent with mode-2/force)"
```

---

### Task 5: runner.py force 模式添加孤立 tool 消息清理

**Why:** 模式二在压缩后调用 `_cleanup_orphan_tool_messages(store)` 清理 DB 残留，但 runner.py 的 force 模式没有。reload 路径只在内存中过滤，DB 残留孤立记录。

**Files:**
- Modify: `agent/runner.py:1180-1234` (reload 路径)

- [ ] **Step 1: 重新设计 reload 路径的 DB 清理逻辑**

**P8 修复**：runner.py 的 `_on_context_high_usage` 运行在同步线程中，不会与 `_chat_lock`（asyncio lock）产生竞争。但 SQLite 层面仍需注意并发写入安全。

**关键设计决策**：不在遍历 `fresh_db_msgs` 的循环内直接修改 DB（避免遍历中修改）。改为先在循环中收集需要清理的 ID，循环结束后统一执行 DB 操作。

在 reload 路径（第 1180 行之后），修改消息遍历循环，收集清理信息后统一 DB 操作：

```python
# 在 reload 路径开头定义 DB 相关变量
_db_path = os.path.join(os.path.expanduser("~"), ".niu", "messages.db")

# 收集需要清理的消息
_orphan_tool_mids = []  # 孤立 tool 消息 ID（需从 DB 删除）
_dangling_tc_updates = []  # 悬空 tool_calls 更新（需更新 DB）

fresh_msgs = []
for msg in fresh_db_msgs:
    d = {
        "role": msg.role,
        "content": msg.content or "",
    }
    if msg.tool_calls:
        valid_tcs = [tc for tc in msg.tool_calls if tc.get("id") in _tool_response_ids]
        if valid_tcs and len(valid_tcs) < len(msg.tool_calls):
            d["tool_calls"] = valid_tcs
            # 收集悬空 tool_calls 清理（循环后统一执行）
            _dangling_tc_updates.append({
                "message_id": msg.id,
                "valid_tcs": valid_tcs,
                "original_count": len(msg.tool_calls),
            })
        elif valid_tcs:
            d["tool_calls"] = valid_tcs
        # 如果所有 tool_calls 都没有响应，不设置 tool_calls（变成纯文本消息）
        elif msg.tool_calls:
            # 收集清空 tool_calls（保留原始 content）
            _dangling_tc_updates.append({
                "message_id": msg.id,
                "valid_tcs": [],
                "original_count": len(msg.tool_calls),
            })
    if msg.tool_call_id:
        if msg.tool_call_id not in _valid_tc_ids:
            logger.warning(f"[Runner] Force: Skipping orphan tool message: tool_call_id={msg.tool_call_id}")
            _orphan_tool_mids.append(msg.id)
            continue
        d["tool_call_id"] = msg.tool_call_id
        _tn = _tc_id_to_name.get(msg.tool_call_id, "")
        if _tn:
            d["name"] = _tn
    fresh_msgs.append(d)

# 统一执行 DB 清理（遍历后执行，避免遍历中修改）
if _orphan_tool_mids or _dangling_tc_updates:
    try:
        import sqlite3
        with sqlite3.connect(_db_path) as _c:
            for mid in _orphan_tool_mids:
                _c.execute("DELETE FROM messages WHERE id = ?", (mid,))
                logger.info(f"[Force-reload] Deleted orphan tool message {mid}")
            for upd in _dangling_tc_updates:
                mid = upd["message_id"]
                if upd["valid_tcs"]:
                    _c.execute(
                        "UPDATE messages SET tool_calls = ? WHERE id = ?",
                        (json.dumps(upd["valid_tcs"], ensure_ascii=False), mid),
                    )
                    logger.info(f"[Force-reload] Cleaned dangling tool_calls for assistant {mid}: {upd['original_count']} -> {len(upd['valid_tcs'])}")
                else:
                    # 清空 tool_calls 但保留原始 content
                    _c.execute("UPDATE messages SET tool_calls = '[]' WHERE id = ?", (mid,))
                    logger.info(f"[Force-reload] Cleared all tool_calls for assistant {mid}")
            _c.commit()
    except Exception as e:
        logger.warning(f"[Force-reload] DB cleanup failed: {e}")
```

**关键改进**（基于审查反馈）：
- 遍历中只收集 ID，不直接修改 DB，避免遍历中修改
- `msg.content or ""` 保留原始内容，不会清空 assistant 消息
- `import sqlite3` 和 `_db_path` 只在循环外定义一次，不重复导入
- 所有 DB 操作在同一个连接中批量执行，效率更高
- 添加 `try/except` 包裹整个 DB 清理，避免 DB 错误影响 reload 主体

- [ ] **Step 2: 语法检查**

Run: `python3 -c "import py_compile; py_compile.compile('agent/runner.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py
git commit -m "fix: runner.py force mode cleans orphan tool messages from DB during reload"
```

---

### Task 6: 游标写入统一使用 `_write_cursor_with_lock`

**Why:** 仅 journal 游标用了文件锁保护，entity/dream/compress 共 7 处裸 `write_text` 写入，可能并发损坏。

**Files:**
- Modify: `niu_api/compat.py:1528,1615,2205,2293,2384,2772`
- Modify: `agent/runner.py:1174`

- [ ] **Step 1: 替换 compat.py 中所有裸 `write_text` 游标写入为 `_write_cursor_with_lock`**

逐一替换以下 6 处（compat.py 内）：

1. 第 1528 行（sleep entity_cursor）
2. 第 1615 行（sleep dream_cursor）
3. 第 2205 行（sleep compress_cursor）— 注意保留 `if compress_integrity_ok` 条件，只替换 `write_text` 调用
4. 第 2293 行（force entity_cursor）
5. 第 2384 行（force dream_cursor）
6. 第 2772 行（force compress_cursor）

每处替换模板：
```python
# Before:
cursor_path.parent.mkdir(parents=True, exist_ok=True)
cursor_path.write_text(json.dumps({...}, ensure_ascii=False, indent=2), encoding="utf-8")

# After:
_write_cursor_with_lock(cursor_path, {...})
```

注意事项：
- 将 `json.dumps({...}, ensure_ascii=False, indent=2)` 中的字典直接传入 `_write_cursor_with_lock`（该函数内部已做 `json.dumps` + `ensure_ascii=False, indent=2`）
- 移除外层的 `parent.mkdir(parents=True, exist_ok=True)`——`_write_cursor_with_lock` 内部已有（第 495 行）
- 第 2205 行的 `if compress_integrity_ok:` 条件必须保留，只替换内部调用

- [ ] **Step 2: 替换 runner.py 中 compress_cursor 写入为 `_write_cursor_with_lock`**

runner.py 已在第 690 行导入了 `_write_cursor_with_lock`，且 `_run_subagent_step` 方法已在使用它。直接调用：

```python
# Before (runner.py ~1174):
compress_cursor_path.parent.mkdir(parents=True, exist_ok=True)
compress_cursor_path.write_text(json.dumps({
    "last_compress_id": new_compress_id,
    "last_compress_at": datetime.now().isoformat(),
}, ensure_ascii=False, indent=2), encoding="utf-8")

# After:
_write_cursor_with_lock(compress_cursor_path, {
    "last_compress_id": new_compress_id,
    "last_compress_at": datetime.now().isoformat(),
})
```

- [ ] **Step 3: 语法检查**

Run: `python3 -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True); py_compile.compile('agent/runner.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py agent/runner.py
git commit -m "fix: all cursor writes use file-lock protection (_write_cursor_with_lock)"
```

---

### Task 7: context-manager.md 添加模式二/三工具链级联说明

**Why:** 工具链完整性规则只标注了"模式一必须遵守"，模式二/三没有提及。虽然程序有级联函数兜底，但 LLM 不知道删除 assistant(tool_calls) 时 tool 输出也会被级联删除，可能导致压缩方案与实际执行结果不符（释放量偏差）。

**Files:**
- Modify: `config/agents/context-manager.md` (模式二和模式三部分)

- [ ] **Step 1: 在模式二 JSON 方案部分添加级联说明**

在 context-manager.md 的模式二"一轮JSON方案"说明中（第 150 行附近），添加：

```markdown
**工具链级联效应**（程序自动处理，但你应了解以正确计算释放量）：
- 如果你删除了一条 assistant(tool_calls) 消息，其所有 tool 输出也会被自动删除（额外释放 token）
- 如果你删除了一条 tool 输出：当父 assistant 是受保护消息时，assistant 会被改写为摘要+清空 tool_calls（tool 输出仍被删除）；当父 assistant 是非保护消息且所有 tool 输出都被删除时，assistant 也会被自动删除
- 如果你更新了一条 assistant(tool_calls) 的内容，其 tool_calls 会被清空且对应 tool 输出会被自动删除
- 因此：你的 JSON 方案中只需列出你决定删除/更新的消息，级联效应由程序处理。但计算释放量时应考虑级联带来的额外释放
```

**P11 修复**：区分了受保护和非保护的 assistant 在 tool 输出被删除时的不同处理。

- [ ] **Step 2: 在模式三部分添加相同说明和策略性删除约束**

在模式三的决策流程说明中（第 200 行附近），添加相同的级联效应说明，并添加约束：

**P10 修复**：添加约束防止策略性删除：

```markdown
**禁止策略性删除**：不得为了间接删除受保护消息的 tool 输出而故意删除其父 assistant(tool_calls)。受保护消息的 tool 输出受程序层面保护——删除受保护的 assistant 会被阻止，但其 tool 输出可能被级联删除。请勿在方案中包含此类意图，程序会自动阻止删除受保护的 assistant 本身。
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/context-manager.md
git commit -m "docs: add tool-chain cascade effect description for mode-2/3 in context-manager.md"
```

---

### Task 8: 修复 `_cascade_tool_chain_deletes` 受保护 assistant 的级联泄漏

**Why:** 当受保护的 assistant(tool_calls) 的某条 tool 输出被删除时，`_cascade_tool_chain_deletes` 正确地跳过了删除受保护的 assistant 本身，但在 Pass 2 中仍然会级联删除该 assistant 的其他 tool 输出（即使这些 tool 输出不在原始删除列表中）。这导致受保护 assistant 的 tool 调用链被意外破坏。

**Files:**
- Modify: `niu_api/compat.py:174-180` (Pass 2 级联逻辑)

- [ ] **Step 1: 修复 Pass 2 中受保护 assistant 的级联泄漏**

在 `_cascade_tool_chain_deletes` 函数的 Pass 2 中（第 174 行附近），当 `_try_add(mid)` 将 assistant 加入 `skipped_protected` 后，不应继续级联删除该 assistant 的其他 tool 输出。修改为：

将第 174 行附近的代码从：
```python
if tc.get("id", "") in deleted_tool_call_ids:
    _try_add(mid)
    # 级联：这个 assistant 的其他 tool_calls 对应的 tool 输出也要删
    for tc2 in tcs:
        tc2_id = tc2.get("id", "")
        if tc2_id and tc2_id not in deleted_tool_call_ids:
            for tool_mid in tc_id_to_tool_mids.get(tc2_id, []):
                _try_add(tool_mid)
    break
```

改为：
```python
if tc.get("id", "") in deleted_tool_call_ids:
    _try_add(mid)
    # 只有当 assistant 未被保护跳过时，才级联删除其其他 tool 输出
    # 受保护的 assistant 需要保持 tool 调用链完整性
    if mid not in skipped_protected:
        for tc2 in tcs:
            tc2_id = tc2.get("id", "")
            if tc2_id and tc2_id not in deleted_tool_call_ids:
                for tool_mid in tc_id_to_tool_mids.get(tc2_id, []):
                    _try_add(tool_mid)
    break
```

**逻辑说明**：
- 当 `_try_add(mid)` 将 assistant 加入 `skipped_protected` 时，`mid not in skipped_protected` 为 False，跳过级联删除其他 tool 输出
- 当 assistant 被正常加入 `added` 时，`mid not in skipped_protected` 为 True，执行级联删除
- 这确保受保护 assistant 的 tool 调用链完整性——只有原始请求删除的 tool 输出会被删除，其他 tool 输出保留
- Pass 3 仍然会正确地收集受保护 assistant 中被删除的 tool_call_id（来自原始删除），并标记为悬空需要清理

- [ ] **Step 2: 语法检查**

Run: `python3 -c "import py_compile; py_compile.compile('niu_api/compat.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 3: Commit**

```bash
git add niu_api/compat.py
git commit -m "fix: _cascade_tool_chain_deletes skips cascade of protected assistant's other tool outputs"
```

---

### Task 9: 压缩失败降级策略

**Why:** `_retry_force_compression` 最多重试 3 次，每次都是相同逻辑。如果保护消息本身占了大半上下文，3 次都会失败，用户陷入无法对话的死锁。

**Files:**
- Modify: `niu_api/chat_queue.py:367-386` (`_retry_force_compression`)
- Modify: `niu_api/compat.py` (`_tidy_context_impl` force 分支)

- [ ] **Step 1: 添加降级逻辑**

在 `_retry_force_compression` 中，当连续 force 压缩失败时（重试 N 次后 token 仍超限），逐步减少 `protect_recent_count`：

```python
async def _retry_force_compression(self, session_id: str, max_retries: int = 3, delay: float = 5.0):
    """重试 force 压缩，逐步放宽保护"""
    from niu_api.compat import _tidy_context_impl, _tidy_lock
    # 降级策略：每次重试减少保护消息数量
    # 第 1 次：默认 protect_recent_count（10）
    # 第 2 次：protect_recent_count = 5
    # 第 3 次：protect_recent_count = 2（最小值，保留最近 1 轮对话）
    degrade_schedule = [None, 5, 2]  # None = 使用默认值

    for attempt in range(max_retries):
        await asyncio.sleep(delay)

        try:
            await asyncio.wait_for(_tidy_lock.acquire(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning(f"[ChatQueue] Force compression retry {attempt+1}/{max_retries}: tidy lock still busy")
            continue

        try:
            request = {"session_id": session_id, "mode": "force"}
            if attempt < len(degrade_schedule) and degrade_schedule[attempt] is not None:
                request["force_protect_recent"] = degrade_schedule[attempt]
                logger.info(f"[ChatQueue] Force compression retry {attempt+1} with degraded protect_recent={degrade_schedule[attempt]}")

            result = await _tidy_context_impl(request=request)

            # 检查压缩后 token 是否降到安全水平
            tokens_after = result.get("tokens_after", 0) if isinstance(result, dict) else 0
            if tokens_after > 0:
                from agent.subagent import _read_context_window_tokens, _read_warning_threshold
                _cw = _read_context_window_tokens()
                _wt = _read_warning_threshold()
                _safe_level = int(_cw * _wt)
                if tokens_after <= _safe_level:
                    logger.info(f"[ChatQueue] Force compression retry {attempt+1} succeeded: tokens_after={tokens_after} <= warning_threshold={_safe_level}")
                    return
                else:
                    logger.warning(f"[ChatQueue] Force compression retry {attempt+1}: tokens_after={tokens_after} still above warning_threshold={_safe_level}")
                    # 继续降级重试，不 return
            else:
                logger.info(f"[ChatQueue] Force compression retry {attempt+1} completed (no tokens_after in result)")
                return
        except Exception as e:
            logger.error(f"[ChatQueue] Force compression retry {attempt+1} failed: {e}")
            # 继续降级重试，不 return——降级策略需要多轮才能生效
        finally:
            _tidy_lock.release()

    logger.error(f"[ChatQueue] All {max_retries} force compression retries exhausted")
```

**关键修改**（基于审查反馈）：
- `except Exception` 分支改为不 `return`，让降级循环继续。降级的目的是逐步减少保护数量，如果第一次重试就因异常退出，降级永远不会生效
- `_read_warning_threshold` 从 `agent.subagent` 导入（已存在，不需要重新定义）
- `tokens_after` 计算方式在 Step 3 中统一为使用 `TokenCalculator`

- [ ] **Step 2: 在 `_tidy_context_impl` 中支持 `force_protect_recent` 参数**

在 `_tidy_context_impl` 的 force 分支中，在 `protect_recent_count = _read_protect_recent_count()` 之后添加：

```python
# 降级策略：允许外部传入更低的保护数量
_force_protect_recent = request.get("force_protect_recent") if isinstance(request, dict) else None
if _force_protect_recent is not None and isinstance(_force_protect_recent, int) and _force_protect_recent >= 1:
    protect_recent_count = min(protect_recent_count, _force_protect_recent)
    logger.info(f"[Tidy] Force: protect_recent_count degraded to {protect_recent_count} (from request)")
```

注意：`min(protect_recent_count, _force_protect_recent)` 确保降级只能减少保护数量，不能增加。

**关键**：降级后的 `protect_recent_count` 必须在整个 force 路径中保持一致。当前 compat.py 第 2661 行有 `protect_recent_count = _read_protect_recent_count()` 重新从配置读取，这会覆盖降级值。必须移除该行，使用已降级的 `protect_recent_count`。否则降级无效——prompt 告诉 LLM 保护更少消息，但程序化执行仍然保护完整的默认数量。

在第 2661 行，将 `protect_recent_count = _read_protect_recent_count()` 删除（该变量已在 force 分支开头通过降级逻辑设置，无需重新读取）。

- [ ] **Step 3: 在 `_tidy_context_impl` force 返回值中添加 `tokens_after`**

当前 force 模式返回 `{"status": "ok", "mode": "force", "tokens_before": display_tokens}`，缺少 `tokens_after`。降级策略需要根据压缩后的 token 数判断是否成功。

**P14 修复**：使用与 `display_tokens` 相同的计算方式（`TokenCalculator`），确保 `tokens_after` 与 `display_tokens` 可比：

```python
# 计算压缩后 token 数（用于降级判断，使用与 display_tokens 相同的计算方式）
tokens_after = display_tokens  # 默认值
try:
    post_messages = await store.get_messages()
    from agent.token_calculator import TokenCalculator
    calc = TokenCalculator.get()
    post_total = 0
    for pm in post_messages:
        try:
            t = calc.count_message_single(pm.role, pm.content or "", tool_calls=getattr(pm, "tool_calls", None))
        except Exception:
            t = max(1, len(pm.content or "") // 2) + 4
        post_total += t
    tokens_after = post_total
except Exception:
    pass

return {"status": "ok", "mode": "force", "tokens_before": display_tokens, "tokens_after": tokens_after}
```

- [ ] **Step 4: 确认 `_read_warning_threshold` 函数已存在**

`_read_warning_threshold` 已从 `agent.subagent` 导入到 `compat.py`（第 16 行确认）。`chat_queue.py` 中需要从 `agent.subagent` 导入该函数（不是从 `niu_api.compat` 导入，因为 compat.py 没有重新导出它）。

- [ ] **Step 5: 语法检查**

Run: `python3 -c "import py_compile; py_compile.compile('niu_api/chat_queue.py', doraise=True); py_compile.compile('niu_api/compat.py', doraise=True); print('OK')"`
Expected: OK

- [ ] **Step 6: Commit**

```bash
git add niu_api/chat_queue.py niu_api/compat.py
git commit -m "feat: add degradation strategy for force compression retries (reduce protect_recent_count)"
```

---

## Self-Review

### Spec Coverage
- C1 (近端压缩策略矛盾) → Task 2 ✅
- C2 (Force prompt 与系统提示词冲突) → Task 1 ✅
- C3 (Force 消息列表缺少 PROTECTED) → Task 1 ✅
- C4 (模式一未设 context_fifo_threshold=0) → Task 4 ✅
- A1 ("未达标视为压缩失败"矛盾) → Task 2 ✅ (合并修复)
- A2 (last_compress_id 矛盾) → Task 3 ✅
- A5 (runner.py 缺少孤立 tool 清理) → Task 5 ✅
- A6 (游标写入未用文件锁) → Task 6 ✅
- A7 (压缩失败无降级策略) → Task 9 ✅
- A3 (工具链完整性指导) → Task 7 ✅
- A4 (reload 只改内存不改 DB) → Task 5 ✅ (合并修复)
- A8 (浮点精度) → 验证后确认为不真实，无需修复 ❌
- C5 (AB-BA 死锁) → 验证后确认为不真实，无需修复 ❌

### Plan Review Round 2 Fixes
- N1 (runner.py force prompt 未覆盖) → Task 1 Step 2/4 ✅
- N2 (runner.py force prompt last_compress_id 保留正确——模式三有游标) → 无需修复 ✅
- P1 (_build_incremental_msg_text "共 N 条新消息") → Task 1 Step 1/2 ✅
- P2 (protected_force_ids 来源等价) → Task 1 Step 3 ✅
- P3 (安全边界优先于模式三) → Task 1 Step 3/4 ✅
- P4 (受保护消息ID已列出) → Task 1 Step 3/4 ✅
- P5 ("更重度压缩"→"按中端规则") → Task 2 Step 2 ✅
- P6 (三层递进结构) → Task 2 Step 1/2 ✅
- P7 (CONTEXT_OVERFLOW是更好的失败模式) → Task 4 Step 1 ✅
- P8 (reload路径DB操作锁) → Task 5 Step 1 ✅
- P9 (_db_path作用域) → Task 5 Step 1 ✅
- P10 (策略性删除约束) → Task 7 Step 2 ✅
- P11 ("或改写"两种情况) → Task 7 Step 1 ✅
- P12 (force_protect_recent实现) → Task 9 Step 2 ✅
- P13 (_tidy_lock逻辑) → Task 9 Step 1 ✅
- P14 (tokens_after用TokenCalculator) → Task 9 Step 3 ✅
- 级联函数受保护assistant泄漏 → Task 8 ✅ (新发现)
- 遍历中修改DB → Task 5 Step 1 ✅ (改为收集后批量执行)
- msg.content清空bug → Task 5 Step 1 ✅ (保留原始content)
- runner.py compress_cursor用_write_cursor_with_lock → Task 6 Step 2 ✅
- 第2205行compress_integrity_ok条件保留 → Task 6 Step 1 ✅
- except Exception: return → Task 9 Step 1 ✅ (改为continue)
- _read_warning_threshold已存在 → Task 9 Step 4 ✅ (从agent.subagent导入)
- Task 2 表格内叠加条件逻辑 → Task 2 Step 1 ✅ (改为表格简洁+段落说明)
- _compress_target措辞可读性 → Task 2 Step 3 ✅ (拆为多行)

### Plan Review Round 3 Fixes
- R3-1 (compat.py msg_list_text 使用过时数据) → Task 1 Step 1 ✅ (移到降级逻辑之后)
- R3-2 (runner.py msg_list_text 使用过时数据) → Task 1 Step 2 ✅ (移到第 899 行之后)
- R3-3 (protect_recent_count 重新读取覆盖降级值) → Task 9 Step 2 ✅ (移除第 2661 行重新读取)

### Plan Review Round 4 Fixes
- R4-1 (删除1443-1452行移除msg_id_set导致sleep模式NameError) → Task 1 Step 1 ✅ (替换为简化计算，保留msg_id_set)
- R4-2 (msg_list_text使用未降级的protect_recent_count) → Task 1 Step 1 ✅ (移到第2489行降级逻辑之后)
- R4-3 (_read_context_window_tokens导入来源不一致) → Task 9 Step 1 ✅ (改为从agent.subagent导入)

### Placeholder Scan
- 所有步骤都包含具体代码，无 TBD/TODO

### Type Consistency
- `_build_incremental_msg_text` 参数签名不变，Task 1 调用方式与模式二一致
- `_write_cursor_with_lock` 接受 `(cursor_path, data: dict)`，与现有 journal 调用一致
- `force_protect_recent` 作为 request dict 中的键传入，与现有 `mode`/`session_id` 模式一致
- `tokens_after` 在 force 模式返回值中新增，使用 TokenCalculator 与 display_tokens 可比
- Task 8 修改 `_cascade_tool_chain_deletes` 不改变函数签名，只修改内部逻辑
