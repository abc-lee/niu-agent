# context-manager 模式三（force 强制压缩）history 列表改造计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 context-manager 模式三（force 强制压缩）的"消息序列化成单条 user message"改造为"直接传 messages 列表（history），每条 content 加简易 idx 前缀"，避免单条 message 超火山方舟单消息 token 上限。与模式二改造对齐。

**Architecture:** 模式三在 `niu_api/compat.py:2488-2563` 的 force 分支内，把 `_build_incremental_msg_text` 调用替换为已存在的 `_build_compress_history`（Task 1 已实现，含孤立 tool 同步排除）。prompt 删除 `msg_list_text` 拼接段，改为引用"上方历史消息"。`run_context_manager_force` 调 `call_subagent` 时新增 `history=_force_history` 参数。idx→真实 ID 映射（`_f_idx_to_id`/`_f_id_to_idx`）由 `_force_msg_ids` 构造，`_build_compress_history` 的 `out_msg_ids` 输出与原 `_force_msg_ids` 语义一致，下游 keep/update/cursor 解析无需改动。

**Tech Stack:** Python 3.11, litellm, 火山方舟 OpenAI 兼容协议

---

## 问题分析

### 当前结构（单消息超限根因，与模式二改造前同构）

`niu_api/compat.py:2488-2563` 的 force 分支：
1. L2491 `_build_incremental_msg_text` 把消息序列化成 `[id:UUID] [idx:N] Ntokens role: content` 纯文本赋给 `msg_list_text`
2. L2547 prompt 拼接 `{msg_list_text}` 进单条 task
3. L2557 `run_context_manager_force` 调 `call_subagent(task=prompt, ...)` 不传 history
4. `call_subagent` 把 task 作为 `initial_user_content` → `agent_runner_loop` append 为**单条 user message**

结果：大量消息合并成单条 user message，触发火山方舟 `Total tokens of image and text exceed max message tokens`（单消息上限）。

### 关键认知

1. **模式三和模式二逻辑相同**，只是触发方式不同（模式二睡眠触发，模式三 CONTEXT_OVERFLOW 或外部主动触发）
2. **模式二已改造成功**（commit 829fb014，端到端验证 316 条 history + 115861 tokens 压缩成功）
3. **`_build_compress_history` 函数已存在**（compat.py L389，Task 1 实现），可直接复用，签名兼容
4. **`call_subagent` 的 `history` 参数已存在**（subagent.py:388），透传链完整
5. **下游解析无需改动**：`_f_idx_to_id` 由 `_force_msg_ids` 构造，`_build_compress_history` 的 `out_msg_ids` 输出与原 `_force_msg_ids` 语义一致
6. **messages/msg_tokens 来源**：force 分支的 context-manager 阶段（L2488+）用的是 **journal 阶段（L2393）重新读取的 messages/msg_tokens**（不是函数顶部 L1439 那份）。entity/dream/journal 三阶段各自重新 `await store.get_messages()` 同步 DB 变更，context-manager 用最后一次 journal 阶段赋值的 `messages`/`msg_tokens` 变量。改造代码用变量名 `messages`/`msg_tokens` 运行时取最新值，正确。

### 行为变化说明（与原 force 逻辑的差异）

1. **孤立 tool 消息不再被 force 清理**：原 `_build_incremental_msg_text` 只排除 PROTECTED，保留孤立 tool（LLM 看到连续 idx 文本，下游 `all_force_idxs - keep_idxs` 会把孤立 tool 列入删除集）。改造后 `_build_compress_history` 同步排除孤立 tool（父 assistant 被 PROTECTED 排除则其 tool 也排除），孤立 tool 不在 `_force_msg_ids` → 不在 `_f_idx_to_id` → 不在 `all_force_idxs` → **不会被显式删除**，残留在 DB。**这是预期行为变化，与模式二对齐**（模式二同样不清理孤立 tool）。理由：history 走 agent_runner_loop 会过滤孤立 tool 导致 idx 错位，源头排除保证 idx 连续。如果需要清理孤立 tool，应由 `_cleanup_orphan_tool_messages`（L2091/2793）独立处理，不依赖 context-manager 压缩。

### 模式三特有内容（改造时必须保留）

1. **cursor 输出行**（prompt L2516 `cursor=15`）：模式三输出三行（keep/update/cursor），模式二只有两行
2. **dream-evolver 安全边界**（prompt L2541）：`idx > _dream_idx_in_force` 的消息只能 update 不能 delete
3. **force_protect_recent 降级**（L2483-2486）：允许外部传入更低的保护数量
4. **chat_lock_already_held** 支持：force 模式常在调用方已持锁时执行
5. **不可跳过**：force 是溢出兜底，不像模式二可以跳过

### 设计原则

1. **直接传 messages 列表**：每条 message 原样，content 加 idx 前缀
2. **复用 `_build_compress_history`**：不新增函数
3. **保留模式三特有内容**：cursor 行、dream 安全边界、force_protect_recent 降级
4. **下游解析不变**：`_f_idx_to_id`/`_f_id_to_idx`/keep/update/cursor 解析全部沿用
5. **不改 `call_subagent` 签名**：history 参数已存在

---

## File Structure

| 文件 | 职责 | 改动类型 |
|------|------|----------|
| `niu_api/compat.py` | L2488-2563 force 分支：替换数据构建 + 改造 prompt + call_subagent 加 history | Modify |
| `tests/test_compress_history.py` | 追加模式三集成测试 | Modify |

---

## Task 1: 改造 force 分支用 history 列表

**Files:**
- Modify: `niu_api/compat.py:2488-2563`
- Test: `tests/test_compress_history.py`

- [ ] **Step 1: 写集成测试 — 模式三传 history 而非序列化文本**

在 `tests/test_compress_history.py` 追加：

```python
def test_mode3_passes_history_to_call_subagent(monkeypatch):
    """模式三（force）应构造 history 列表传给 call_subagent，而非序列化文本塞进 task。"""
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

    # mock runner 控制 usage_percent（force 模式不依赖 usage，但 _tidy_context_impl 仍会读取）
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module
    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()  # 180K tokens，模拟溢出
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    # mock call_subagent 捕获参数，返回 keep=/update=/cursor= 三行
    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["agent_name"] = agent_name
            captured["task"] = kwargs.get("task", "")
            captured["history"] = kwargs.get("history")
            return "keep=1,2\ncursor=2\nupdate="
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_target_threshold", lambda: 0.3, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)

    # 调用 _tidy_context_impl force 模式
    request = {"session_id": "test", "mode": "force"}
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
    # task 应含 cursor= 输出说明（模式三特有）
    assert "cursor=" in captured["task"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_history.py::test_mode3_passes_history_to_call_subagent -v`
Expected: FAIL（当前 `run_context_manager_force` 不传 history 参数，task 含 `[id:` 序列化文本）

- [ ] **Step 3: 改造 force 分支数据构建（L2490-2496）**

Read `niu_api/compat.py:2488-2497` 确认当前代码。

当前代码（L2490-2496）：
```python
            _force_msg_ids = []
            msg_list_text = _build_incremental_msg_text(
                messages, "", _force_msg_ids, msg_tokens,
                end_cursor_id=None, protect_recent=protect_recent_count,
                exclude_protected=True
            )
            msg_list_text = msg_list_text.replace("条新消息", "条消息", 1)
```

改为（用 `_build_compress_history` 替代，`_force_msg_ids` 通过 `out_msg_ids` 收集）：
```python
            _force_msg_ids = []
            _force_history, _ = _build_compress_history(
                messages, msg_tokens,
                out_msg_ids=_force_msg_ids,
                protect_recent=protect_recent_count,
                exclude_protected=True,
            )
```

注意：删除 `msg_list_text = msg_list_text.replace(...)` 行（history 模式下不需要文本替换）。

- [ ] **Step 4: 保留 idx 映射不变（L2499-2509）**

Read `niu_api/compat.py:2498-2510` 确认。这段代码构造 `_f_idx_to_id`/`_f_id_to_idx` + `_dream_idx_in_force`，**保持不变**——`_force_msg_ids` 由 `_build_compress_history` 的 `out_msg_ids` 填充，顺序与原 `_build_incremental_msg_text` 一致，映射逻辑无需改动。

- [ ] **Step 5: 改造 prompt（L2511-2551）**

Read `niu_api/compat.py:2511-2552` 确认完整 prompt。

当前 prompt 含两处引用 `msg_list_text` 和 `message_count`：
- L2535 `- 总消息数：{message_count}`
- L2544-2548 `--- 以下为消息列表数据 ---\n共 {message_count} 条消息\n{msg_list_text}\n--- 消息列表数据结束 ---`

改造：删除 L2544-2548 的消息列表数据段，改为引用"上方历史消息"。L2535 的 `message_count` 改为 `len(_force_history)`（受保护消息已排除，实际 history 条数）。

把当前 prompt（L2511-2551）改为：

```python
            prompt = f"""CRITICAL: 你只有一轮机会完成所有压缩决策。禁止调用任何工具（包括 write、delete_messages、update_message、bash 等），直接在回复内容中输出压缩方案。

输出格式（直接回复，不调用任何工具）：
keep=1,3,5-10,15
update=2|摘要内容;11|摘要内容
cursor=15

说明：
- keep= 后列出所有保留的消息 idx（用逗号分隔，连续的可用短横线如 5-10）
- update= 后列出需要压缩为摘要的消息（idx|摘要内容，多条用分号分隔）
- update 中的 idx 必须也在 keep 列表中（保留但压缩内容）
- cursor= 后填操作范围内 idx 最大的、且仍存在的消息 idx
- 未列在 keep 中的消息将被删除
- 只输出这三行，不要输出其他内容

压缩规则（必须遵守）：
- 按事务合并：属于同一件事的多轮交互（用户要求→工具调用→结果），合并为一条摘要
- 远端摘要格式："用户要求X，最终Y"（只保留意图和结果，丢弃过程）
- 近端摘要格式："用户要求X，调用Z工具，得到Y"（保留关键工具和输出）
- role=tool 的工具输出：不需要放入keep，会被程序自动删除
- 纯确认回复（"好的""明白了""谢谢"）：不需要放入keep
- 不在keep中的消息会被程序自动删除，所以有价值的对话必须放进keep或update

当前上下文状态：
- 参与压缩的消息数：{len(_force_history)}（受保护消息已排除）
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{target_tokens}
- 需释放至少 {display_tokens - target_tokens} tokens
- 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}

上方历史消息每条开头带 [idx:N] Ntokens 前缀，共 {len(_force_history)} 条。
role=tool 的工具输出会被程序自动删除，不需要放入 keep。

安全边界：idx > {_dream_idx_in_force} 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
注：受保护消息已从列表中排除，无需处理。

请按照【模式三】执行压缩决策，安全边界优先于模式三决策流程。
REMINDER: 禁止调用任何工具，直接在回复中输出 keep=/update=/cursor= 三行。"""
```

**关键改动**：
- 删除 `--- 以下为消息列表数据 ---` 段（含 `{msg_list_text}`）
- 新增"上方历史消息每条开头带 [idx:N] Ntokens 前缀，共 {len(_force_history)} 条"引用
- L2535 `总消息数：{message_count}` 改为 `参与压缩的消息数：{len(_force_history)}（受保护消息已排除）`
- 保留 cursor= 输出说明、dream 安全边界、压缩规则等模式三特有内容

- [ ] **Step 6: 改造 call_subagent 加 history 参数（L2556-2563）**

Read `niu_api/compat.py:2556-2563` 确认。

当前代码：
```python
            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    context_fifo_threshold=0,
                )
```

改为（新增 `history=_force_history`）：
```python
            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    context_fifo_threshold=0,
                    history=_force_history,  # 直接传 messages 列表，避免单条 user message 超限
                )
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_compress_history.py -v`
Expected: 全部 PASS（7 个已有测试 + 1 个新模式三集成测试 = 8 个）

- [ ] **Step 8: 验证现有测试不破坏**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/ 2>&1 | tail -20`
Expected: 无新增 FAIL（预存的 17 个 FAIL 与基线一致）

- [ ] **Step 9: 临时提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/compat.py tests/test_compress_history.py
git commit -m "feat(compat): mode-3 force compression uses history list instead of serialized text

模式三（force 强制压缩）改造：L2490 _build_incremental_msg_text 替换为
_build_compress_history，prompt 删除 msg_list_text 拼接改为引用上方历史消息，
run_context_manager_force 通过 call_subagent 的 history 参数传入。

与模式二改造对齐，避免单条 message 超火山方舟单消息 token 上限。
保留模式三特有内容：cursor= 输出行、dream 安全边界、force_protect_recent
降级、chat_lock_already_held 支持。下游 keep/update/cursor 解析无需改动。"
```

---

## Task 2: 端到端验证（真实 force 压缩触发）

**Files:**
- 无文件修改，纯验证

- [ ] **Step 1: 启动程序，积累上下文到 80%+ 触发模式三**

用户执行：
1. `./niu` 启动程序
2. 持续对话，直到上下文使用率 ≥ 80%（日志显示 `usage=X.X%`）
3. 主 Agent 返回 `CONTEXT_OVERFLOW` 时自动触发 force 压缩
4. 或观察日志 `[Tidy] Force:` 相关行

- [ ] **Step 2: 检查压缩请求日志结构**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python3 -c "
import json, glob, os, datetime
files = sorted(glob.glob('logs/raw_http/' + datetime.date.today().strftime('%Y%m%d') + '/*_request.json'))
for f in reversed(files[-20:]):
    with open(f) as fh:
        req = json.load(fh)
    sys_content = req['messages'][0].get('content','')
    if isinstance(sys_content, str) and '记忆压缩器' in sys_content:
        msgs = req['messages']
        # 检查是否是 force 模式（task 含 cursor=）
        task_content = ''
        for m in msgs:
            if m.get('role') == 'user':
                c = m.get('content','')
                if isinstance(c, str) and 'cursor=' in c:
                    task_content = c
                    break
        if not task_content:
            continue
        print(f'=== 找到 force context-manager 请求: {f} ===')
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
        # 确认 history 有多条（不是 2 条 = 1 system + 1 巨大 user）
        assert len(msgs) > 2, f'history 未展开，消息数 {len(msgs)}'
        print(f'✅ history 已展开（{len(msgs)} 条消息）')
        break
"
```

Expected:
- 消息数 > 2（history 列表展开）
- 最大 user message 长度 < 100000（压缩指令很小）
- task 含 `cursor=`（模式三特有）

- [ ] **Step 3: 验证压缩成功执行**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "Force.*Parsed\|Force.*Deleted\|Force.*Compression plan" logs/api_stderr.log 2>/dev/null | tail -5 || echo "检查 niu_api stderr 日志"`

Expected: 看到 `[Tidy] Force: ...` 相关的解析和执行日志（keep/delete/update/cursor 计数正常）。

- [ ] **Step 4: 验证无单消息超限错误**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep "exceed max message tokens" logs/api_stderr.log 2>/dev/null | tail -3 || echo "无单消息超限错误"`

Expected: 不再出现 `Total tokens of image and text exceed max message tokens`。

- [ ] **Step 5: 最终提交（清理调试代码，如有）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git status
git add -A
git commit -m "feat(compress): context-manager mode-3 force uses message list

模式三 force 强制压缩改造完成，与模式二对齐：
- _build_compress_history 构造 history 列表（复用模式二函数）
- prompt 引用上方历史消息，保留 cursor= / dream 安全边界
- call_subagent 传 history 参数
- 避免单条 message 超火山方舟单消息 token 上限"
```

---

## 自审检查

### 1. Spec 覆盖

- force 分支数据构建替换 → Task 1 Step 3 ✅
- idx 映射保留不变 → Task 1 Step 4 ✅
- prompt 改造（删 msg_list_text + 引用 history + 保留 cursor/dream）→ Task 1 Step 5 ✅
- call_subagent 加 history → Task 1 Step 6 ✅
- 模式三特有内容保留（cursor 行、dream 边界、force_protect_recent）→ Task 1 Step 5 ✅
- 下游解析不变 → 不改 L2572-2629 ✅
- 端到端验证 → Task 2 ✅

### 2. Placeholder 扫描

无 TBD/TODO。所有步骤包含具体代码。

### 3. 类型一致性

- `_build_compress_history(messages, msg_tokens, out_msg_ids, protect_recent, exclude_protected) -> tuple[list[dict], dict[int, str]]`: 已存在（模式二 Task 1 实现）
- `_force_history: list[dict]`: 与模式二 `compress_history` 同构
- `_force_msg_ids: list[str]`: 由 `out_msg_ids` 填充，与原 `_build_incremental_msg_text` 语义一致
- `_f_idx_to_id: dict[int, str]`: 不变
- `call_subagent(..., history=list|None)`: 已存在

### 4. 边界条件

- `_force_history` 为空（所有消息被 PROTECTED 排除）→ 下游 L2572+ 的 `if not _force_msg_ids` 类似检查会处理（与原逻辑一致）
- force_protect_recent 降级 → `protect_recent_count` 由 L2483-2486 计算，传入 `_build_compress_history`，正确
- chat_lock_already_held → 不涉及 history 改造，不受影响
- **孤立 tool 行为变化**：改造后 force 不再显式删除孤立 tool（父 assistant 被 PROTECTED 排除时其 tool 也被排除，不在 `_force_msg_ids`/`_f_idx_to_id`/`all_force_idxs`）。与模式二对齐。孤立 tool 清理由 `_cleanup_orphan_tool_messages` 独立负责（L2793，force 分支末尾仍会调用）。详见"行为变化说明"。

### 5. 向后兼容

- `_build_incremental_msg_text` 保留（其他阶段仍用）
- 模式二改造不受影响（独立分支）
- `call_subagent` history 参数已存在
- 下游 keep/update/cursor 解析不变
- force_protect_recent / chat_lock_already_held / dream 安全边界全部保留

### 6. 风险点

- **模式三 prompt 比 mode2 复杂**（多 cursor 行 + dream 边界 + 完整规则）：改造后引用式 prompt 仍需让 LLM 理解"上方历史消息"。Task 2 端到端验证确认。
- **`message_count` 变量**：原 prompt L2535 用 `message_count`（全量消息数），改造后用 `len(_force_history)`（排除保护后的条数）。如果 L2535 之外还有地方用 `message_count`，需确认。grep 确认。
- **`_dream_idx_in_force` 计算**：基于 `_f_id_to_idx`，而 `_f_id_to_idx` 由 `_force_msg_ids` 构造。`_build_compress_history` 的 `out_msg_ids` 顺序与原一致，`_dream_idx_in_force` 计算正确。

### 7. 不改动的部分

- `_build_compress_history` 函数（模式二 Task 1 已实现）
- `_build_incremental_msg_text` 函数（其他阶段用）
- `call_subagent` / `_run_agent_loop` / `agent_runner_loop` 签名
- 模式二分支（L1776-1920）
- idx 映射构造（`_f_idx_to_id`/`_f_id_to_idx`）
- 下游 keep/update/cursor 解析
- 压缩执行逻辑（chat_lock / DB 删除/更新 / 级联清理）
- dream 安全边界 / force_protect_recent / chat_lock_already_held
