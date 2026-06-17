# 压缩系统三合一修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复强制压缩的三个严重bug：token计数不一致、非法异步触发路径、保护标签覆盖工具输出

**Architecture:** 压缩只在 agent_loop 工具循环内部同步触发；保护标签排除 role=tool 消息；CONTEXT_OVERFLOW 返回值优先用真实 prompt_tokens

**Tech Stack:** Python, asyncio, loguru

---

## File Structure

| File | Responsibility |
|------|---------------|
| `agent/generic/agent_loop.py` | 修改1: CONTEXT_OVERFLOW 返回值用真实 token |
| `agent/runner.py` | 修改1: 清理无用导入; 修改3: protected_force_ids + prompt |
| `niu_api/compat.py` | 修改2: _should_auto_tidy 返回 False; 修改3: 3处保护逻辑 |
| `niu_api/chat.py` | 修改2: 删除2处 _check_and_trigger_auto_tidy 调用 |
| `niu_api/chat_queue.py` | 修改2: 删除1处 _check_and_trigger_auto_tidy 调用 |
| `agent/context_manager.py` | 修改2: should_compress 返回 False |
| `config/agents/context-manager.md` | 修改3: 保护规则提示词更新 |
| `tests/test_subagent_overflow.py` | 修改2: _should_auto_tidy 测试更新 |

---

### Task 1: CONTEXT_OVERFLOW 返回值使用真实 prompt_tokens

**Files:**
- Modify: `agent/generic/agent_loop.py:326`
- Modify: `agent/runner.py:759`

- [ ] **Step 1: 修改 agent_loop.py 第326行**

将：
```python
"tokens_used": count_messages_tokens(messages),
```
改为：
```python
"tokens_used": last_prompt_tokens if last_prompt_tokens > 0 else count_messages_tokens(messages),
```

- [ ] **Step 2: 清理 runner.py 无用导入**

将第756-760行：
```python
from niu_api.compat import (
    _build_incremental_msg_text,
    _truncate_task_for_subagent,
    _estimate_total_tokens,
)
```
改为：
```python
from niu_api.compat import (
    _build_incremental_msg_text,
    _truncate_task_for_subagent,
)
```

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); ast.parse(open('agent/runner.py').read()); print('syntax ok')"`

- [ ] **Step 4: Commit**

```bash
git add agent/generic/agent_loop.py agent/runner.py
git commit -m "fix(compress): use real prompt_tokens in CONTEXT_OVERFLOW return value"
```

---

### Task 2: 关闭所有非工具循环的压缩触发路径

**Files:**
- Modify: `niu_api/compat.py:263-276` (_should_auto_tidy)
- Modify: `niu_api/compat.py:652-655` (chat_session 调用)
- Modify: `niu_api/chat.py:430-434` (SSE 调用)
- Modify: `niu_api/chat.py:535-539` (sync 调用)
- Modify: `niu_api/chat_queue.py:358-360` (queue 调用)
- Modify: `agent/context_manager.py:106-125` (should_compress)

- [ ] **Step 1: 禁用 _should_auto_tidy**

将 `niu_api/compat.py` 第263-276行：
```python
def _should_auto_tidy(current_tokens: int, context_window_tokens: int = 0) -> bool:
    """
    判断是否应该触发自动增量整理。

    仅当上下文使用率 >= 80% 时触发。
    工具输出的超长问题由 agent_loop 中的截断机制处理，
    不再用 50K 增量阈值触发整理（该阈值在重启后容易误触发）。
    """
    if current_tokens <= 0:
        return False
    if context_window_tokens <= 0:
        return False
    usage_ratio = current_tokens / context_window_tokens
    return usage_ratio >= 0.80
```
改为：
```python
def _should_auto_tidy(current_tokens: int, context_window_tokens: int = 0) -> bool:
    """已禁用：压缩只在 agent_loop 工具循环中同步触发，不在对话后异步触发。"""
    return False
```

注意：`_check_and_trigger_auto_tidy` 和 `_run_auto_tidy` 函数定义保留，因为 `/api/context/tidy` 端点可能间接使用它们。4个调用点删除后它们成为死代码，但保留定义避免 import 错误。

- [ ] **Step 2: 删除 chat_session 中的 _check_and_trigger_auto_tidy 调用**

将 `niu_api/compat.py` 第652-655行：
```python
        else:
            # 正常：异步触发增量整理检查（不阻塞）
            if full_reply.strip():
                await _check_and_trigger_auto_tidy(store)

        return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)
```
改为：
```python
        return ChatResponse(reply=full_reply, session_id="default", message_id=message_id)
```

- [ ] **Step 3: 删除 chat SSE 中的 _check_and_trigger_auto_tidy 调用**

将 `niu_api/chat.py` 第430-434行：
```python
        else:
            # 正常：异步触发增量整理检查（不阻塞）
            if full_reply.strip():
                from niu_api.compat import _check_and_trigger_auto_tidy
                await _check_and_trigger_auto_tidy(store)

        # Send final message
```
改为：
```python
        # Send final message
```

- [ ] **Step 4: 删除 chat sync 中的 _check_and_trigger_auto_tidy 调用**

将 `niu_api/chat.py` 第535-539行：
```python
        else:
            # 正常：异步触发增量整理检查（不阻塞）
            if full_reply.strip():
                from niu_api.compat import _check_and_trigger_auto_tidy
                await _check_and_trigger_auto_tidy(store)

        return ChatResponse(session_id=session_id, reply=full_reply, message_id=message_id)
```
改为：
```python
        return ChatResponse(session_id=session_id, reply=full_reply, message_id=message_id)
```

- [ ] **Step 5: 删除 chat_queue 中的 _check_and_trigger_auto_tidy 调用**

将 `niu_api/chat_queue.py` 第358-360行：
```python
        elif full_reply.strip():
            from niu_api.compat import _check_and_trigger_auto_tidy
            await _check_and_trigger_auto_tidy(store)
```
删除这3行。

- [ ] **Step 6: 禁用 context_manager.py 的 should_compress**

将 `agent/context_manager.py` 第106-125行：
```python
    def should_compress(self, messages: List[Dict[str, Any]]) -> bool:
        """
        判断是否需要压缩上下文

        Args:
            messages: 消息列表

        Returns:
            是否需要压缩
        """
        # 条件1: 消息数量超过限制
        if len(messages) > self.max_messages:
            return True

        # 条件2: Token 数量超过 warningThreshold
        tokens = self.count_tokens_simple(messages)
        if tokens > self.max_tokens * self._warning_threshold:
            return True

        return False
```
改为：
```python
    def should_compress(self, messages: List[Dict[str, Any]]) -> bool:
        """已禁用：压缩只在 agent_loop 工具循环中同步触发。"""
        return False
```

- [ ] **Step 7: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); ast.parse(open('niu_api/chat.py').read()); ast.parse(open('niu_api/chat_queue.py').read()); ast.parse(open('agent/context_manager.py').read()); print('syntax ok')"`

- [ ] **Step 8: Commit**

```bash
git add niu_api/compat.py niu_api/chat.py niu_api/chat_queue.py agent/context_manager.py
git commit -m "fix(compress): disable all non-tool-loop compress triggers — only agent_loop can trigger"
```

---

### Task 3: 保护标签排除工具输出 — _build_incremental_msg_text

**Files:**
- Modify: `niu_api/compat.py:141-144`

- [ ] **Step 1: 修改 _build_incremental_msg_text 保护标签逻辑**

当前逻辑是"在最后N个位置中加标签"，应改为"对最后N条user/assistant消息加标签"以与 protected_force_ids 语义一致。

将第82-150行的 `_build_incremental_msg_text` 函数，在 `protect_recent` 参数处理后增加预计算逻辑。在循环开始前，先统计尾部 user/assistant 消息并记录哪些位置需要保护：

将第141-144行：
```python
        # protect_recent: 对最后 N 条消息加 [PROTECTED] 标签
        protected_label = ""
        if protect_recent > 0 and rel_pos >= total_count - protect_recent:
            protected_label = "[PROTECTED] "
```
改为：
```python
        # protect_recent: 对最后 N 条 user/assistant 消息加 [PROTECTED] 标签（不保护 role=tool 的工具输出）
        protected_label = ""
        if protect_recent > 0 and _protected_positions is not None and rel_pos in _protected_positions:
            protected_label = "[PROTECTED] "
```

同时在第132行 `total_count = len(range_messages_with_pos)` 之后、第133行 `for rel_pos, ...` 循环之前插入预计算：
```python
    # 预计算保护位置：从尾部向前找 N 条 user/assistant 消息的相对位置
    _protected_positions = None
    if protect_recent > 0:
        _protected_positions = set()
        _count = 0
        for rp in range(total_count - 1, -1, -1):
            _, m = range_messages_with_pos[rp]
            if getattr(m, "role", "") in ("user", "assistant"):
                _protected_positions.add(rp)
                _count += 1
                if _count >= protect_recent:
                    break
```

- [ ] **Step 2: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('syntax ok')"`

- [ ] **Step 3: Commit**

```bash
git add niu_api/compat.py
git commit -m "fix(compress): PROTECTED label excludes tool output — only user/assistant messages"
```

---

### Task 4: 保护标签排除工具输出 — protected_force_ids（3处）

**Files:**
- Modify: `agent/runner.py:1058-1061`
- Modify: `niu_api/compat.py:1748-1751`
- Modify: `niu_api/compat.py:1258-1270`

- [ ] **Step 1: 修改 runner.py 的 protected_force_ids**

将第1058-1061行：
```python
                    # 保护最近 N 条消息
                    protect_recent_count = _read_protect_recent_count()
                    if protect_recent_count > 0 and len(fresh_messages) > protect_recent_count:
                        protected_force_ids = {getattr(m, "id", "") for m in fresh_messages[-protect_recent_count:]}
```
改为：
```python
                    # 保护最近 N 条 user/assistant 消息（不保护 role=tool 的工具输出）
                    protect_recent_count = _read_protect_recent_count()
                    if protect_recent_count > 0 and len(fresh_messages) > protect_recent_count:
                        _pids = []
                        for m in reversed(fresh_messages):
                            if getattr(m, "role", "") in ("user", "assistant"):
                                _pids.append(getattr(m, "id", ""))
                            if len(_pids) >= protect_recent_count:
                                break
                        protected_force_ids = set(_pids)
```

- [ ] **Step 2: 修改 compat.py force 模式的 protected_force_ids**

将第1748-1751行：
```python
            # 程序层面排除保护范围内的消息 ID
            protect_recent_count = _read_protect_recent_count()
            if protect_recent_count > 0 and len(fresh_messages) > protect_recent_count:
                protected_force_ids = {getattr(m, "id", "") for m in fresh_messages[-protect_recent_count:]}
```
改为：
```python
            # 程序层面排除保护范围内的消息 ID（只保护 user/assistant，不保护 tool 输出）
            protect_recent_count = _read_protect_recent_count()
            if protect_recent_count > 0 and len(fresh_messages) > protect_recent_count:
                _pids = []
                for m in reversed(fresh_messages):
                    if getattr(m, "role", "") in ("user", "assistant"):
                        _pids.append(getattr(m, "id", ""))
                    if len(_pids) >= protect_recent_count:
                        break
                protected_force_ids = set(_pids)
```

- [ ] **Step 3: 修改 compat.py sleep 模式的 protected_ids**

将第1269-1270行：
```python
                # 构建保护消息 UUID 列表
                protected_ids = compress_msg_ids[-protect_recent_count:] if len(compress_msg_ids) > protect_recent_count else compress_msg_ids[:]
```
改为：
```python
                # 构建保护消息 UUID 列表（只含 user/assistant 消息，不含 tool 输出）
                _pids = []
                for i in range(len(compress_msg_ids) - 1, -1, -1):
                    _mid = compress_msg_ids[i]
                    _m = next((m for m in messages if getattr(m, "id", "") == _mid), None)
                    if _m and getattr(_m, "role", "") in ("user", "assistant"):
                        _pids.insert(0, _mid)
                    if len(_pids) >= protect_recent_count:
                        break
                protected_ids = _pids if _pids else compress_msg_ids[:]
```

- [ ] **Step 4: 验证语法**

Run: `python -c "import ast; ast.parse(open('agent/runner.py').read()); ast.parse(open('niu_api/compat.py').read()); print('syntax ok')"`

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py niu_api/compat.py
git commit -m "fix(compress): protected_force_ids excludes tool output — scan from tail, count only user/assistant"
```

---

### Task 5: 更新提示词中的保护规则说明

**Files:**
- Modify: `agent/runner.py:959`
- Modify: `config/agents/context-manager.md:25-28`

- [ ] **Step 1: 更新 runner.py force 模式 prompt 保护规则**

在 prompt f-string 构建之前（约第939行之后），需要先读取 `protect_recent_count`，因为 f-string 中要引用它。将 `protect_recent_count = _read_protect_recent_count()` 添加到 prompt 构建代码之前。

然后将第959行：
```
      保护规则：操作开始时记录 idx 最大的 10 条消息的 id（UUID），这些消息绝不删除（按 id 判断，不受后续 idx 变化影响）。
```
改为（用变量替换硬编码"10"）：
```
      保护规则：操作开始时记录 idx 最大的 {protect_recent_count} 条 user/assistant 消息的 id（UUID），这些消息绝不删除。role=tool 的工具输出不在保护范围内，可以删除或压缩（按 id 判断，不受后续 idx 变化影响）。
```

注意：`_read_protect_recent_count` 已在第765行导入。第1059行的原有 `protect_recent_count = _read_protect_recent_count()` 保留不动（重复读取无副作用，保持逻辑清晰）。

- [ ] **Step 2: 更新 context-manager.md 提示词保护规则**

Read `config/agents/context-manager.md`，找到所有包含硬编码"10"的保护规则说明。需要修改的位置：
- 第25-28行的 `[PROTECTED] 保护标签` 段落
- 第143行的"记录 idx 最大的 10 条消息"
- 第193行的"若总消息数 ≤ 10"

将 `[PROTECTED] 保护标签` 段落改为：
```
**[PROTECTED] 保护标签**：
- 带有 `[PROTECTED]` 标签的消息是最近的 user/assistant 对话消息，**绝对不可删除或压缩**
- role=tool 的工具输出消息不会标记 `[PROTECTED]`，可以安全删除或压缩
- 程序层面也会兜底保护标记了 `[PROTECTED]` 的消息（即使你误操作，程序也会阻止）
- 保护数量由配置决定，默认 10 条（只计 user/assistant 消息）
```

其他包含硬编码"10"的位置，改为"配置指定的数量"或"默认10条"的动态描述。

注意：第193行"若总消息数 ≤ 10：保护所有消息"中的"10"是一个独立的安全阈值（总消息太少时不删除任何内容），与 `protect_recent_count` 是不同概念。此处保留硬编码"10"不变。

- [ ] **Step 2b: 更新 compat.py force 模式 prompt 保护规则**

compat.py 第1648行也有相同的硬编码保护规则：
```
            保护规则：操作开始时记录 idx 最大的 10 条消息的 id（UUID），这些消息绝不删除
```
同样需要修改。在 compat.py 的 force 模式 prompt 构建之前（约第1628行之后），添加 `protect_recent_count = _read_protect_recent_count()`，然后将第1648行改为：
```
            保护规则：操作开始时记录 idx 最大的 {protect_recent_count} 条 user/assistant 消息的 id（UUID），这些消息绝不删除。role=tool 的工具输出不在保护范围内，可以删除或压缩。
```

- [ ] **Step 3: 更新测试文件**

Read `tests/test_subagent_overflow.py`，找到引用 `_should_auto_tidy` 的测试用例（约第604-624行）。`_should_auto_tidy` 现在永远返回 False，这些测试需要修改：
- 测试 `_should_auto_tidy` 返回 True 的用例 → 改为测试返回 False（或删除）
- 测试 `_should_auto_tidy` 返回 False 的用例 → 保留（行为不变）

Read `tests/test_p1/test_context_manager.py`，找到第87行 `manager.should_compress(messages)` 断言为 True 的测试。`should_compress` 现在永远返回 False，此测试需要修改：
- 断言 `should_compress` 返回 True 的用例 → 改为断言返回 False（或删除）

- [ ] **Step 4: Commit**

```bash
git add agent/runner.py niu_api/compat.py config/agents/context-manager.md tests/test_subagent_overflow.py tests/test_p1/test_context_manager.py
git commit -m "fix(compress): update prompt protection rules — exclude tool output, use dynamic count"
```

---

## Verification

1. 启动程序，正常对话到上下文使用率 > 80%
2. 确认压缩在工具循环中同步触发（日志显示 `[Context] Proactive compress`），对话阻塞
3. 确认不再出现 `[AutoTidy]` 异步触发日志
4. 确认保护标签只出现在 user/assistant 消息上，tool 消息无保护标签
5. 确认 CONTEXT_OVERFLOW 返回值中 `tokens_used` 使用真实 prompt_tokens
6. 休眠5分钟后触发整理仍然正常工作（前端 SLEEP → /api/context/tidy）
