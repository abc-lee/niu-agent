# 修复上下文压缩方向反转和保护失效

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复睡眠模式上下文压缩的三个 bug：1) 只压缩近端不压缩远端（方向反转），2) 受保护消息被更新/压缩（保护失效），3) 非强制压缩量过大（无量化目标）

**Architecture:** 保留睡眠模式压缩逻辑不变，修复三个具体问题：扩大压缩范围让远端可压、加强保护机制让受保护消息不可动、在提示词中加量化目标控制压缩量。同时修复审查发现的 5 个衍生问题。

**Tech Stack:** Python (FastAPI, asyncio), SQLite (消息存储), LLM 子Agent (context-manager)

---

## 修改文件

| 文件 | 修改内容 |
|------|----------|
| `niu_api/compat.py` | 1) 模式二时仅在游标过旧时扩大范围；2) 事后校验增加更新检测+回滚+阻止游标推进；3) 提示词加动态量化目标 |
| `config/agents/context-manager.md` | 加强 [PROTECTED] 描述、添加受保护消息在单元内的排除规则、更新游标范围描述 |

---

## Bug 根因

### Bug 1：方向反转——只压缩近端

`_build_incremental_msg_text()` 只传入 `[last_compress_id, new_dream_id]` 范围内的消息。游标之前的老消息永远不在范围内，永远不会被压缩。

**修复**：模式二时，如果游标位置在消息列表前半段（说明远端有大量未压缩内容），将游标重置为空使范围从头开始。否则仍用增量范围，避免摘要级联衰减。

### Bug 2：受保护消息被更新

事后校验只检查删除不检查更新。LLM 把 `[PROTECTED]` 理解为"不删除"，但仍更新内容。

**修复**：事后校验增加更新检测+回滚。检测到受保护消息被删除时阻止游标推进（游标不写入）。

### Bug 3：非强制压缩量过大

模式二提示词没有量化目标。LLM 压缩了97%。

**修复**：基于 `targetThreshold` 计算动态目标（将上下文降至 targetThreshold），而非固定20%。

### 审查发现的衍生问题

- **截断风险**：全量消息可能被截断砍掉近端。解决方案：限制全量范围大小，不传超过 context_window*0.4 tokens 的消息。
- **[PROTECTED] 与压缩规则冲突**："不可修改内容"与"保留 idx 最小→改写为摘要"矛盾。解决方案：在规则中明确受保护消息从单元中排除，剩余消息正常压缩。
- **游标范围描述不一致**：context-manager.md 第 16 行说"只传入增量范围"，但模式二改为可全量。需更新描述。

---

### Task 1: 扩大模式二压缩范围（仅在游标过旧时）

**Files:**
- Modify: `niu_api/compat.py:1418-1421`

当前代码：
```python
compress_msg_text = _build_incremental_msg_text(
    messages, last_compress_id, compress_msg_ids, msg_tokens,
    end_cursor_id=new_dream_id, protect_recent=protect_recent_count
)
```

修改为：模式二时，仅在游标过旧（位置 < 消息总数50%）时重置游标为空，否则仍用增量范围。同时限制全量范围的 token 总量不超过 context_window*0.4，避免截断砍掉近端消息。

```python
# 模式二：如果游标位置在消息列表前半段（远端有大量未压缩内容），重置游标从头开始；
# 否则仍用增量范围，避免已压缩的摘要被级联再压缩
_compress_cursor = last_compress_id
if usage_percent >= 50 and last_compress_id:
    cursor_pos = next((i for i, m in enumerate(messages) if getattr(m, "id", "") == last_compress_id), -1)
    if cursor_pos < 0 or cursor_pos < len(messages) * 0.5:
        _compress_cursor = ""  # 游标太旧或无效，从头开始

compress_msg_text = _build_incremental_msg_text(
    messages, _compress_cursor, compress_msg_ids, msg_tokens,
    end_cursor_id=new_dream_id, protect_recent=protect_recent_count
)

# 限制全量范围的 token 总量，避免截断砍掉近端消息
# 如果消息文本超过 context_window*0.4 tokens，从远端截断（保留近端）
_compress_window = int(_read_context_window_tokens() * 0.4)
if compress_msg_text and _estimate_text_tokens(compress_msg_text) > _compress_window:
    # 从开头截断远端消息，保留近端（消息列表在 prompt 末尾）
    compress_msg_text = _truncate_preserving_tail(compress_msg_text, _compress_window)
```

需要新增两个辅助函数：

```python
def _estimate_text_tokens(text: str) -> int:
    """粗略估算文本 token 数（中文约1.5字/token，英文约4字/token，取中间值2字/token）"""
    return len(text) // 2

def _truncate_preserving_tail(text: str, max_tokens: int) -> str:
    """截断文本，保留末尾近端消息（远端从开头截断）。
    消息列表在 prompt 末尾，开头是远端(idx小的)，末尾是近端(idx大的)。
    截断远端保留近端，确保 LLM 能看到需要保护的消息。"""
    max_chars = max_tokens * 2  # 反向估算字符数
    if len(text) <= max_chars:
        return text
    # 保留末尾近端部分，截断开头远端
    kept_tail = text[-max_chars:]
    # 找到第一个完整的消息行（以 [id: 开头）
    first_line_pos = kept_tail.find("[id:")
    if first_line_pos > 0:
        kept_tail = kept_tail[first_line_pos:]
    # 更新消息计数（首行 "共 N 条新消息"）
    line_count = kept_tail.count("[id:")
    return f"共约 {line_count} 条消息（远端部分已省略）\n\n" + kept_tail
```

- [ ] **Step 1: 修改 `_tidy_context_impl` 中压缩范围逻辑 + 添加截断保护**

- [ ] **Step 2: 添加 `_estimate_text_tokens` 和 `_truncate_preserving_tail` 辅助函数**

在 `compat.py` 中 `_build_incremental_msg_text` 函数之后添加这两个函数。

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 4: 提交**

```bash
git add niu_api/compat.py
git commit -m "fix: expand compress range for mode-2 when cursor is stale, with truncation protection"
```

---

### Task 2: 加强受保护消息的事后校验（检测更新+回滚+阻止游标推进）

**Files:**
- Modify: `niu_api/compat.py:1500-1509`（事后校验）
- Modify: `niu_api/compat.py:1511-1517`（游标写入逻辑，约在事后校验之后）

当前事后校验只检查删除且只记录 warning：
```python
if protected_ids:
    try:
        post_msgs = await store.get_messages()
        post_ids = {getattr(m, "id", "") for m in post_msgs}
        for pid in protected_ids:
            if pid not in post_ids:
                logger.warning(f"[Tidy] PROTECTED message {pid} was deleted by context-manager!")
    except Exception as e:
        logger.warning(f"[Tidy] Failed to verify protected messages: {e}")
```

替换为：检测删除和更新，更新则回滚，删除则阻止游标推进。

```python
compress_integrity_ok = True  # 压缩完整性标记，用于决定是否推进游标
if protected_ids:
    try:
        # 构建受保护消息的原始内容映射（内存中的 messages 列表未被子Agent修改）
        protected_originals = {}
        for pid in protected_ids:
            _m = next((m for m in messages if getattr(m, "id", "") == pid), None)
            if _m:
                protected_originals[pid] = getattr(_m, "content", "") or ""

        post_msgs = await store.get_messages()
        post_ids = {getattr(m, "id", "") for m in post_msgs}
        post_content_map = {getattr(m, "id", ""): (getattr(m, "content", "") or "") for m in post_msgs}

        for pid in protected_ids:
            if pid not in post_ids:
                logger.error(f"[Tidy] PROTECTED message {pid} was deleted by context-manager! Cannot restore (add_message would disorder sequence). Blocking cursor advance.")
                compress_integrity_ok = False
            elif pid in protected_originals and pid in post_content_map:
                original = protected_originals[pid]
                current = post_content_map[pid]
                if original != current:
                    logger.warning(f"[Tidy] PROTECTED message {pid} was modified by context-manager! Rolling back content...")
                    await store.update_message(pid, original)
    except Exception as e:
        logger.warning(f"[Tidy] Failed to verify protected messages: {e}")
```

然后在游标写入逻辑中，只有 `compress_integrity_ok` 为 True 时才推进游标：

需要找到游标写入位置（在事后校验之后，大约第 1511-1517 行），在写入前检查 `compress_integrity_ok`：

```python
if compress_integrity_ok:
    # 正常推进游标
    _write_cursor_json(...)
else:
    # 受保护消息被删除，不推进游标，下次压缩会重新处理
    logger.warning("[Tidy] Skipping cursor advance due to protected message integrity failure")
```

- [ ] **Step 1: 替换事后校验代码，添加 compress_integrity_ok 标记**

- [ ] **Step 2: 在游标写入前检查 compress_integrity_ok**

需要先 Read 游标写入的具体代码位置和格式，然后添加条件判断。

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 4: 提交**

```bash
git add niu_api/compat.py
git commit -m "fix: add update detection+rollback for protected messages, block cursor advance on integrity failure"
```

---

### Task 3: 加强提示词中 [PROTECTED] 的描述 + 单元排除规则

**Files:**
- Modify: `config/agents/context-manager.md:16`（游标范围描述）
- Modify: `config/agents/context-manager.md:25-29`（[PROTECTED] 保护标签）
- Modify: `config/agents/context-manager.md:86`（模式一安全边界）
- Modify: `config/agents/context-manager.md:100`（模式二操作范围）
- Modify: `config/agents/context-manager.md:112`（模式二安全边界）
- Modify: `config/agents/context-manager.md:193`（重要约束）
- Modify: `niu_api/compat.py:1442`（prompt 中 [PROTECTED] 描述）

**3a: 第 16 行——更新游标范围描述**

当前：
```
程序只传入增量范围内的消息（compress_cursor 到 dream_cursor_new 之间的消息），你只需处理收到的全部消息。
```

替换为：
```
程序传入范围内的消息：模式一传入增量范围（compress_cursor 到 dream_cursor_new 之间），模式二可能传入全量范围（从头到 dream_cursor_new，当远端有大量未压缩内容时）。你只需处理收到的全部消息。
```

**3b: 第 25-29 行——[PROTECTED] 保护标签**

当前：
```
**[PROTECTED] 保护标签**：
- 带有 `[PROTECTED]` 标签的 user/assistant 消息是最近的重要消息，**绝对不可删除或压缩**
- role=tool 的工具输出不在保护范围内，可以删除或压缩
- 程序层面也会兜底保护这些消息（即使你误操作，程序也会阻止）
- 保护数量由配置决定，默认 10 条
```

替换为：
```
**[PROTECTED] 保护标签**：
- 带有 `[PROTECTED]` 标签的 user/assistant 消息是最近的重要消息，**完全不可动（不可删除、不可压缩、不可修改内容、不可合并）**
- 对 [PROTECTED] 消息的唯一合法操作是什么都不做，保持原样
- role=tool 的工具输出不在保护范围内，可以删除或压缩
- 程序层面也会兜底保护这些消息：内容修改会被自动回滚，删除会阻止游标推进
- 保护数量由配置决定，默认 10 条
- **[PROTECTED] 消息在会话单元内的处理**：受保护消息从单元中排除，不参与压缩；排除后剩余消息 >= 2 条则正常压缩，剩余 < 2 条则跳过该单元
```

**3c: 第 86 行——模式一安全边界**

当前：
```
- 带 [PROTECTED] 标签的消息不可删除或压缩
```

替换为：
```
- 带 [PROTECTED] 标签的消息完全不可动（不可删除、不可压缩、不可修改内容、不可合并）
- 如果合并单元内 idx 最小的消息是 [PROTECTED] 的，跳过该消息，选择 idx 第二小的作为合并锚点
```

**3d: 第 100 行——模式二操作范围**

当前：
```
**操作范围**：last_compress_id 对应idx < idx ≤ last_dream_evolve_id 对应idx 的消息（先从消息列表中找到游标UUID对应的idx，再用idx确定范围；若 last_compress_id 为空则从 idx=0 开始）
```

替换为：
```
**操作范围**：收到的全部消息（程序已裁切好范围：模式一为增量范围，模式二可能为全量范围）。你只需处理收到的全部消息，不需要自行判断范围边界。
```

**3e: 第 112 行——模式二安全边界**

当前：
```
- 带 [PROTECTED] 标签的消息不可删除或压缩
```

替换为：
```
- 带 [PROTECTED] 标签的消息完全不可动（不可删除、不可压缩、不可修改内容、不可合并）
- 如果单元内 idx 最小的消息是 [PROTECTED] 的，排除该消息，对剩余消息执行压缩；排除后剩余 < 2 条则跳过该单元
```

**3f: 第 193 行——重要约束**

当前：
```
- 带 [PROTECTED] 标签的消息绝不删除或压缩
```

替换为：
```
- 带 [PROTECTED] 标签的消息完全不可动（不可删除、不可压缩、不可修改内容、不可合并）
```

**3g: compat.py 第 1442 行的 prompt**

当前：
```
以下消息已标注 [PROTECTED]，不可删除或压缩：
```

替换为：
```
以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并），在单元内应排除不参与压缩：
```

- [ ] **Step 1: 修改 context-manager.md 的 7 处修改**

- [ ] **Step 2: 修改 compat.py prompt 中的 [PROTECTED] 描述**

- [ ] **Step 3: 提交**

```bash
git add config/agents/context-manager.md niu_api/compat.py
git commit -m "fix: strengthen [PROTECTED] as completely immutable, add unit exclusion rule, update range description"
```

---

### Task 4: 模式二提示词添加动态量化压缩目标

**Files:**
- Modify: `niu_api/compat.py:1438-1449`

当前提示词没有量化目标。替换为基于 `targetThreshold` 的动态目标：

```python
# 模式二量化目标：基于 targetThreshold 计算动态目标，而非固定20%
_compress_target = ""
if usage_percent >= 50:
    target_threshold = _read_target_threshold()
    target_tokens = int(display_tokens * target_threshold)
    suggest_release = max(display_tokens - target_tokens, int(display_tokens * 0.1))
    _compress_target = f"\n压缩目标：建议释放约 {suggest_release} tokens，将上下文从 {usage_percent:.1f}% 降至约 {target_threshold*100:.0f}%。优先压缩远端（idx 小的）消息；如果远端释放量不足目标，继续压缩近端非保护消息直到达标\n"

prompt = f"""系统进入睡眠状态。

当前上下文：{display_tokens} tokens（{usage_percent:.1f}%）
{_compress_target}以下消息已标注 [PROTECTED]，完全不可动（不可删除、不可压缩、不可修改内容、不可合并），在单元内应排除不参与压缩：
保护消息ID: {json.dumps(protected_ids)}

消息列表：
{compress_msg_text}

请按照【{compress_mode}】的规则处理。处理完成后，在报告末尾用 JSON 格式报告：{{"last_compress_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}}
**必须推进游标**：即使没有需要处理的内容，也必须输出 idx 最大的消息的 UUID。"""
```

- [ ] **Step 1: 修改提示词，添加动态量化目标**

- [ ] **Step 2: 验证 `_read_target_threshold` 函数是否存在**

需要确认 compat.py 中是否已有 `_read_target_threshold()` 函数。如果没有，需要从 `config/user-config.json` 的 `context.targetThreshold` 读取（默认 0.50）。

- [ ] **Step 3: 验证语法**

Run: `python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('OK')"`

- [ ] **Step 4: 提交**

```bash
git add niu_api/compat.py
git commit -m "fix: add dynamic compress target based on targetThreshold for mode-2"
```

---

## 审查问题修复记录

| # | 严重度 | 问题 | 修复方案 |
|---|--------|------|----------|
| C2 | Critical | [PROTECTED] "不可修改内容"与模式一合并规则冲突（idx最小是受保护消息时无法合并） | 在模式一安全边界添加排除规则：跳过受保护消息，选idx第二小 |
| C3 | Critical | [PROTECTED] "不可修改内容"与模式二"保留idx最小→改写为摘要"冲突 | 在模式二安全边界添加排除规则：排除受保护消息，剩余正常压缩 |
| A4 | Important | 全量消息截断砍掉近端消息（LLM看不到需要保护的消息） | 新增 `_truncate_preserving_tail` 保留末尾近端，截断开头远端；限制全量范围 token 总量 |
| A3 | Important | 模式二每次全量重处理导致摘要级联衰减 | 仅在游标过旧（位置<50%）时重置游标，否则仍用增量范围 |
| D1 | Important | 20%固定目标不适应压力水平 | 改为基于 targetThreshold 的动态目标 |
| B3 | Important | 受保护消息被删除后游标仍推进，消息永久丢失 | 添加 compress_integrity_ok 标记，删除时阻止游标推进 |
| C1 | Minor | "完全不可动"4处措辞不一致 | 统一为括号版本 |
| E2 | 遗漏 | context-manager.md第16行"只传入增量范围"与模式二全量矛盾 | 更新第16行描述 |

---

## 验证

1. 启动程序，正常使用直到上下文超过 50%
2. 观察睡眠模式压缩日志：
   - 当游标过旧时，压缩范围应包含远端消息（idx 从1开始）
   - 当游标较新时，仍用增量范围（避免摘要级联衰减）
   - 受保护消息不应被修改（事后校验无 warning/error）
   - 压缩量应基于 targetThreshold 动态计算
3. 检查压缩后消息列表：
   - 远端消息应被压缩为摘要
   - 近端10条 user/assistant 消息应完整保留
   - 受保护消息在单元内被排除，其余消息正常压缩
