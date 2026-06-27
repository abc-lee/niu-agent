# Exclude PROTECTED Messages from Compress Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 不把 PROTECTED 消息内容传给 context-manager，从源头消除 LLM 违规操作 PROTECTED 消息的风险。

**Architecture:** 在 `_build_incremental_msg_text` 中新增 `exclude_protected` 参数，当启用时从输出文本和 `out_msg_ids` 中排除 PROTECTED 消息。compress 的三种模式（sleep 模式一/二、force 模式三）调用时传入 `exclude_protected=True`，同时清理 prompt 中的 PROTECTED 声明文本和压缩后防御逻辑。

**Tech Stack:** Python 3.11+, pytest

---

## 当前问题

context-manager 收到 PROTECTED 消息的完整内容，只靠 `[PROTECTED]` 标签和文字指令指望 LLM 不动它们。但：
1. 模式一：MCP 工具（update_message/delete_messages）无程序拦截，LLM 可以直接操作 PROTECTED 消息
2. 模式二/三：程序层面有拦截，但传 PROTECTED 内容给 LLM 仍然浪费 token 且增加违规风险
3. 压缩后防御逻辑（L2034-2058）是"事后补救"——被删除的消息无法恢复

## 修改的文件

| 文件 | 改动 |
|------|------|
| `niu_api/compat.py` | `_build_incremental_msg_text` 增加 `exclude_protected` 参数；3 处 compress 调用传入 `exclude_protected=True`；清理 3 处 prompt 中的 PROTECTED 声明；简化压缩后防御逻辑 |
| `config/agents/context-manager.md` | 清理 PROTECTED 相关指令（模式一/二/三） |

---

### Task 1: `_build_incremental_msg_text` 增加 `exclude_protected` 参数

**Files:**
- Modify: `niu_api/compat.py:303-383`

当前函数签名：
```python
def _build_incremental_msg_text(messages, last_cursor_id: str, out_msg_ids: list, msg_tokens: list | None = None, end_cursor_id: str | None = None, protect_recent: int = 0) -> str:
```

- [ ] **Step 1: 修改函数签名，增加 `exclude_protected` 参数**

将 L303 的签名改为：
```python
def _build_incremental_msg_text(messages, last_cursor_id: str, out_msg_ids: list, msg_tokens: list | None = None, end_cursor_id: str | None = None, protect_recent: int = 0, exclude_protected: bool = False) -> str:
```

- [ ] **Step 2: 在消息遍历循环中实现排除逻辑**

当前 L366-378 的循环对每条消息都执行 `out_msg_ids.append(msg_id)` 和 `lines.append(...)`。修改为：当 `exclude_protected=True` 且消息在 `_protected_positions` 中时，跳过该消息（不加入 `out_msg_ids`，不加入 `lines`）。

将 L366-378 替换为：
```python
    display_idx = 0
    for rel_pos, (orig_pos, msg) in enumerate(range_messages_with_pos):
        msg_id = getattr(msg, "id", "") or ""
        content = msg.content or ""
        token_annotation = ""
        if msg_tokens and (start + orig_pos) < len(msg_tokens):
            token_annotation = f"{msg_tokens[start + orig_pos]}tokens "
        # protect_recent: 对最后 N 条 user/assistant 消息加 [PROTECTED] 标签（不保护 role=tool 的工具输出）
        protected_label = ""
        if protect_recent > 0 and _protected_positions is not None and rel_pos in _protected_positions:
            if exclude_protected:
                continue  # 排除 PROTECTED 消息：不加入 out_msg_ids 和 lines
            protected_label = "[PROTECTED] "
        display_idx += 1
        out_msg_ids.append(msg_id)
        lines.append(f"[id:{msg_id}] [idx:{display_idx}] {token_annotation}{msg.role}: {protected_label}{content}")
```

关键改动：用连续递增的 `display_idx` 替代 `original_idx = start + orig_pos + 1`。当 `exclude_protected=True` 跳过 PROTECTED 消息后，`display_idx` 保证连续编号，与模式二/三的 `_idx_to_id`/`_f_idx_to_id` 映射一致（这些映射基于 `out_msg_ids` 的连续位置）。当 `exclude_protected=False` 时，`display_idx` 与 `original_idx` 行为相同（无消息跳过，编号连续）。

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: add exclude_protected param to _build_incremental_msg_text"
```

---

### Task 2: compress 三处调用传入 `exclude_protected=True`

**Files:**
- Modify: `niu_api/compat.py:1666-1668` (sleep 模式一/二)
- Modify: `niu_api/compat.py:2385-2388` (force 模式三)

- [ ] **Step 1: 修改 sleep 模式 compress 调用**

当前 L1666-1668：
```python
            compress_msg_text = _build_incremental_msg_text(
                messages, _compress_cursor, compress_msg_ids, msg_tokens,
                end_cursor_id=_end_cursor, protect_recent=protect_recent_count
            )
```

改为：
```python
            compress_msg_text = _build_incremental_msg_text(
                messages, _compress_cursor, compress_msg_ids, msg_tokens,
                end_cursor_id=_end_cursor, protect_recent=protect_recent_count,
                exclude_protected=True
            )
```

- [ ] **Step 2: 修改 force 模式 compress 调用**

当前 L2385-2388：
```python
            msg_list_text = _build_incremental_msg_text(
                messages, "", _force_msg_ids, msg_tokens,
                end_cursor_id=None, protect_recent=protect_recent_count
            )
```

改为：
```python
            msg_list_text = _build_incremental_msg_text(
                messages, "", _force_msg_ids, msg_tokens,
                end_cursor_id=None, protect_recent=protect_recent_count,
                exclude_protected=True
            )
```

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "feat: compress calls pass exclude_protected=True"
```

---

### Task 3: 清理 prompt 中的 PROTECTED 声明

**Files:**
- Modify: `niu_api/compat.py:1738-1760` (模式二 prompt)
- Modify: `niu_api/compat.py:1762-1771` (模式一 prompt)
- Modify: `niu_api/compat.py:2441-2445` (force 模式三 prompt)

现在 PROTECTED 消息已从输入中排除，prompt 不再需要声明保护规则。

- [ ] **Step 1: 清理模式一 prompt**

注意：L1721-1728 的 `protected_ids` 计算**必须保留**，Task 4 Step 2 的简化完整性检查仍依赖它。

当前 L1762-1771：
```python
                    prompt = f"""系统进入睡眠状态。

当前上下文：{display_tokens} tokens（{usage_percent:.1f}%）
{_compress_target}以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并），在单元内应排除不参与压缩：
保护消息ID: {json.dumps(protected_ids)}

消息列表：
{compress_msg_text}

请按照【{compress_mode}】的规则处理。"""
```

改为：
```python
                    prompt = f"""系统进入睡眠状态。

当前上下文：{display_tokens} tokens（{usage_percent:.1f}%）
{_compress_target}消息列表（已排除受保护消息）：
{compress_msg_text}

请按照【{compress_mode}】的规则处理。"""
```

- [ ] **Step 2: 清理模式二 prompt**

当前 L1745：
```python
[PROTECTED]标记的消息不可动。直接回复两行文本，不要调用任何工具，不要输出其他任何内容：
```

改为：
```python
直接回复两行文本，不要调用任何工具，不要输出其他任何内容：
```

- [ ] **Step 3: 清理 force 模式三 prompt**

当前 L2441-2445：
```python
保护消息 idx：{_protected_force_idxs}
受保护消息已在上方列出，这些消息绝不删除。安全边界优先于模式三决策流程。

安全边界：先从消息列表中找到 last_dream_evolve_id={new_dream_id} 对应的 idx，idx > 该idx 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
保护规则：操作开始时记录 idx 最大的 {protect_recent_count} 条 user/assistant 消息，这些消息绝不删除。role=tool 的工具输出不在保护范围内，可以删除或压缩。
```

改为：
```python
安全边界：idx > {_dream_idx_in_force} 的消息（dream-evolver 未提取知识），不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
注：受保护消息已从列表中排除，无需处理。
```

其中 `_dream_idx_in_force` 在 prompt 前计算，**替换 L2409 的 `_protected_force_idxs` 行**：
```python
            # dream-evolver 安全边界 idx（排除后列表中的位置）
            if not new_dream_id:
                _dream_idx_in_force = 0
            else:
                _dream_idx_in_force = _f_id_to_idx.get(new_dream_id, len(_force_msg_ids))
```

- `new_dream_id` 在 `_f_id_to_idx` 中：正常映射到排除后列表的 idx
- `new_dream_id` 不在 `_f_id_to_idx` 中（指向 PROTECTED 消息，已在排除列表之后）：回退到 `len(_force_msg_ids)`（最大 idx），表示排除列表中所有非 PROTECTED 消息都是"已提取知识"，可以安全删除
- `new_dream_id` 为空：回退到 0，表示所有消息都是"未提取知识"（与原始行为一致）

- [ ] **Step 4: 删除 force 模式中 `protected_force_ids` 死代码**

当前 L2391-2399 计算 `protected_force_ids = _f_pids`。prompt 清理后此列表不再被引用（L2614-2622 重新计算同名局部变量）。删除 L2391-2399 的 `_f_pids` 计算和 `protected_force_ids = _f_pids` 赋值。

- [ ] **Step 5: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 6: Commit**

```bash
git add niu_api/compat.py
git commit -m "refactor: remove PROTECTED declarations from compress prompts"
```

---

### Task 4: 简化压缩后防御逻辑

**Files:**
- Modify: `niu_api/compat.py:2024-2032` (游标 PROTECTED 回退)
- Modify: `niu_api/compat.py:2034-2058` (压缩后完整性检查)

现在 PROTECTED 消息不在 `compress_msg_ids` 中，游标不可能推进到 PROTECTED 消息，压缩后也不需要检测 PROTECTED 消息是否被修改。

- [ ] **Step 1: 删除游标 PROTECTED 回退逻辑（L2024-2032）**

当前：
```python
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

替换为：
```python
                        # PROTECTED 消息已从 compress_msg_ids 中排除，游标不可能指向 PROTECTED 消息
```

- [ ] **Step 2: 简化压缩后完整性检查（L2034-2058）**

当前逻辑检测 PROTECTED 消息是否被删除或修改。现在 PROTECTED 消息不在输入中，context-manager 无法操作它们。但保留一个轻量检查作为防御性编程——只检查 PROTECTED 消息是否仍存在（不检查内容修改，因为不可能被修改）。

将 L2034-2058 替换为：
```python
                    compress_integrity_ok = True
                    if protected_ids:
                        try:
                            post_msgs = await store.get_messages()
                            post_ids = {getattr(m, "id", "") for m in post_msgs}
                            for pid in protected_ids:
                                if pid not in post_ids:
                                    logger.error(f"[Tidy] PROTECTED message {pid} missing after compress! Blocking cursor advance.")
                                    compress_integrity_ok = False
                                    break
                        except Exception as e:
                            logger.warning(f"[Tidy] Failed to verify protected messages: {e}")
                            compress_integrity_ok = False
```

- [ ] **Step 3: 明确保留模式二/三执行阶段拦截**

模式二 L1900-1911 和模式三 L2614-2630 的 `protected_set` / `protected_force_ids` 执行阶段拦截**必须保留**。虽然 PROTECTED 消息不在输入中，但 LLM 可能幻觉出 PROTECTED 消息的 UUID（从之前会话记忆或其他来源），这些 UUID 会通过 `existing_ids` 检查（PROTECTED 消息确实存在于 DB 中），导致被误操作。执行阶段拦截是防御 UUID 幻觉的必要安全层。

在这两处拦截代码前增加注释：
```python
# 防御 UUID 幻觉：PROTECTED 消息已从输入中排除，但 LLM 可能幻觉出其 UUID
```

- [ ] **Step 4: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add niu_api/compat.py
git commit -m "refactor: simplify PROTECTED defense after exclusion"
```

---

### Task 5: 清理 context-manager.md 中 PROTECTED 指令

**Files:**
- Modify: `config/agents/context-manager.md`

- [ ] **Step 1: 修改游标机制中的 PROTECTED 说明**

当前 L30-36：
```
**[PROTECTED] 保护标签**：
- 带有 `[PROTECTED]` 标签的 user/assistant 消息是最近的重要消息，**完全不可动（不可删除、不可压缩、不可修改内容、不可合并）**
- 对 [PROTECTED] 消息的唯一合法操作是什么都不做，保持原样
- role=tool 的工具输出不在保护范围内，可以删除或压缩
- 程序层面也会兜底保护这些消息：内容修改会被自动回滚，删除会阻止游标推进
- 保护数量由配置决定，默认 10 条
- **[PROTECTED] 消息在会话单元内的处理**：受保护消息从单元中排除，不参与压缩；排除后剩余消息 >= 2 条则正常压缩，剩余 < 2 条则跳过该单元
```

改为：
```
**[PROTECTED] 保护标签**：
- 受保护消息已由程序从输入中排除，你不会收到这些消息
- **禁止调用 get_messages** — 它会返回包含受保护消息的完整列表，操作这些消息会导致压缩失败和游标阻塞
- 无需关注保护机制，正常处理收到的所有消息即可
- 保护数量由配置决定，默认 10 条
```

- [ ] **Step 2: 修改模式一安全边界**

当前 L94-98：
```
**安全边界**：
- 带 [PROTECTED] 标签的消息完全不可动（不可删除、不可压缩、不可修改内容、不可合并）
- 如果合并单元内有 [PROTECTED] 消息，排除所有 [PROTECTED] 消息；排除后剩余 >= 2 条则正常压缩（选 idx 最小的非保护消息作为合并锚点），剩余 < 2 条则跳过该单元
```

改为：
```
**安全边界**：
- 受保护消息已从输入中排除，无需特殊处理
```

- [ ] **Step 3: 修改模式二安全边界**

当前 L183-184：
```
**安全边界**：
- 带 [PROTECTED] 标签的消息完全不可动（不可删除、不可压缩、不可修改内容、不可合并）
- 如果单元内有 [PROTECTED] 消息，排除所有 [PROTECTED] 消息；排除后剩余 >= 2 条则正常压缩，剩余 < 2 条则跳过该单元
```

改为：
```
**安全边界**：
- 受保护消息已从输入中排除，无需特殊处理
```

- [ ] **Step 4: 修改模式三安全边界**

当前 L255：
```
**安全边界**：
- 绝不删除操作开始时记录的 prompt 中指定数量的保护消息（按 id 判断，不受后续 idx 变化影响）
```

改为：
```
**安全边界**：
- 受保护消息已从输入中排除，无需特殊处理
```

- [ ] **Step 5: 修改重要约束中的 PROTECTED 规则**

当前 L267-268：
```
- 带 [PROTECTED] 标签的消息完全不可动（不可删除、不可压缩、不可修改内容、不可合并）
  - 若总消息数 ≤ 10：保护所有消息，仅允许压缩为摘要，不允许删除
```

改为：
```
- 受保护消息已由程序从输入中排除，你不会收到这些消息
```

- [ ] **Step 6: Commit**

```bash
git add config/agents/context-manager.md
git commit -m "refactor: simplify PROTECTED instructions in context-manager prompt"
```

---

### Task 6: 更新测试

**Files:**
- Modify: `tests/test_tidy_cursor.py`

- [ ] **Step 1: 添加 `exclude_protected` 测试**

在 `tests/test_tidy_cursor.py` 末尾添加：

```python
def test_exclude_protected_removes_protected_messages():
    """exclude_protected=True 时，PROTECTED 消息不出现在输出文本和 out_msg_ids 中"""
    messages = make_messages(10)  # 10 条消息
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "", out_ids,
        protect_recent=3,  # 最后 3 条 user/assistant 标记为 PROTECTED
        exclude_protected=True
    )
    # PROTECTED 消息不应在 text 中出现
    assert "[PROTECTED]" not in text
    # out_ids 不应包含最后 3 条消息的 ID
    all_ids = [getattr(m, "id", "") for m in messages]
    protected_ids = all_ids[-3:]  # 最后 3 条是 user/assistant（make_messages 交替 user/assistant）
    for pid in protected_ids:
        assert pid not in out_ids
    # 非保护消息应在 out_ids 中
    non_protected_ids = all_ids[:-3]
    for npid in non_protected_ids:
        assert npid in out_ids


def test_exclude_protected_false_keeps_protected_messages():
    """exclude_protected=False 时，PROTECTED 消息正常出现在输出中"""
    messages = make_messages(10)
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "", out_ids,
        protect_recent=3,
        exclude_protected=False
    )
    assert "[PROTECTED]" in text
    # 所有消息 ID 都在 out_ids 中
    all_ids = [getattr(m, "id", "") for m in messages]
    assert set(out_ids) == set(all_ids)


def test_exclude_protected_without_protect_recent_is_noop():
    """protect_recent=0 时，exclude_protected 无效（没有消息被标记为 PROTECTED）"""
    messages = make_messages(10)
    out_ids_exclude = []
    out_ids_normal = []
    _build_incremental_msg_text(messages, "", out_ids_exclude, protect_recent=0, exclude_protected=True)
    _build_incremental_msg_text(messages, "", out_ids_normal, protect_recent=0, exclude_protected=False)
    assert out_ids_exclude == out_ids_normal
```

- [ ] **Step 1b: 添加 tool 消息混合测试**

```python
def test_exclude_protected_with_tool_messages():
    """包含 tool 消息时，exclude_protected 只排除 PROTECTED 的 user/assistant 消息"""
    messages = [
        FakeMessage(id="uuid-0", role="user", content="用户消息 0"),
        FakeMessage(id="uuid-1", role="assistant", content="助手消息 1", tool_calls=[{"id": "tc1"}]),
        FakeMessage(id="uuid-2", role="tool", content="工具输出 2", tool_call_id="tc1"),
        FakeMessage(id="uuid-3", role="user", content="用户消息 3"),
        FakeMessage(id="uuid-4", role="assistant", content="助手消息 4"),
    ]
    out_ids = []
    text = _build_incremental_msg_text(
        messages, "", out_ids,
        protect_recent=1,  # 保护最后 1 条 user/assistant = uuid-4
        exclude_protected=True
    )
    # uuid-4 被排除（PROTECTED），uuid-2（tool）不被排除
    assert "uuid-4" not in out_ids
    assert "uuid-2" in out_ids
    assert "uuid-0" in out_ids
    assert "uuid-1" in out_ids
    assert "uuid-3" in out_ids
```

- [ ] **Step 2: 运行测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_tidy_cursor.py::test_exclude_protected_removes_protected_messages tests/test_tidy_cursor.py::test_exclude_protected_false_keeps_protected_messages tests/test_tidy_cursor.py::test_exclude_protected_without_protect_recent_is_noop tests/test_tidy_cursor.py::test_exclude_protected_with_tool_messages -v`
Expected: 4 passed

- [ ] **Step 3: Commit**

```bash
git add tests/test_tidy_cursor.py
git commit -m "test: add exclude_protected tests for _build_incremental_msg_text"
```

---

## 每步验证

每个 Task 完成后：
1. `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"` — 语法检查
2. `python -c "from niu_api.compat import _build_incremental_msg_text"` — import 不报错

## 功能验证

1. 启动程序 → 发几条消息 → 等 sleep 触发 → 检查日志中 context-manager 收到的消息列表不含 `[PROTECTED]` 标签
2. 检查游标文件 `~/.niu/last_compress.json` 推进正常
3. 检查 PROTECTED 消息在 DB 中未被修改或删除
