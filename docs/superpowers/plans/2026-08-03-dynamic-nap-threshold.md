# 动态阈值算法 + now bug 防御性修复

## 问题背景

### 问题1：小憩模式触发阈值算法不合理
当前 `_calc_dream_trigger_threshold` 写死 `AVG_TURN_TOKENS=12000`，200K 窗口算出 raw_threshold=7 被保底 10 兜住，导致所有 ≤200K 窗口都走保底 10 轮。用户期望 200K 窗口约 15 轮触发一次。

### 问题2：build_subagent_system_segments 无 fallback
`now` bug（提交 `77b30e68` 引入，`a85efcef` 修复）暴露了 `call_subagent` 中 `build_subagent_system_segments` 调用位于所有 try/except 之前，system prompt 构建失败直接拖垮整个子 Agent，没有 fallback。

## 方案

### Task 1：动态阈值算法（替换 `_calc_dream_trigger_threshold`）

**文件**：`agent/runner.py`

**新函数签名**（模块级函数，与旧函数一致）：
```python
def _calc_dream_trigger_threshold_dynamic(
    context_window: int,
    post_compress_msgs: list,       # 压缩游标之后的消息
    post_compress_tokens: list[int], # 压缩游标之后消息的 token 估算
) -> int:
```

**关键设计：阈值计算和触发判断统一使用压缩游标**

压缩后的消息被裁剪/替换过，token 含量与正常对话完全不同，不能用来估算每轮开销。
只取 `last_compress_id` 游标之后的消息（即上次压缩后新产生的、未经压缩的真实对话）来算平均值。
如果从未压缩过（游标为空或游标指向的消息不在 DB 中），则用全量消息。

**算法**：
```python
# 1. 数轮数（user 消息数）—— 只数压缩游标后的
turn_count = sum(1 for m in post_compress_msgs if getattr(m, "role", "") == "user")

# 2. 算总消息 token —— 只算压缩游标后的
total_msg_tokens = sum(post_compress_tokens)

# 3. 算平均每轮 token
if turn_count >= 3:
    avg_tokens_per_turn = total_msg_tokens / turn_count
else:
    # 样本不足，直接返回保底值，跳过 avg 计算
    return 10

# 4. 安全下限：avg 至少 1000 tokens
avg_tokens_per_turn = max(1000, avg_tokens_per_turn)

# 5. 增量预算 = 窗口 × 30%
incremental_budget = context_window * 0.30

# 6. 阈值 = 增量预算 / 每轮开销
threshold = int(incremental_budget / avg_tokens_per_turn)

# 7. 下限 10 轮，上限 50 轮
return max(10, min(50, threshold))
```

**200K 验证**（用 `_recalc_msg_stats` 口径）：
- 19 轮对话，用 `_recalc_msg_stats` 计算消息 token 总和（含 content + tool_calls，不含 system prompt）
- 实际 avg 取决于消息内容，预计在 2000-4000 tokens/轮区间
- threshold = 60000 / 3000 ≈ 20，在 10-50 区间内
- 若 avg 偏高（工具密集型，avg=6000）→ threshold = 60000 / 6000 = 10（下限兜底）
- 符合用户期望的"约 15 轮"区间

**删除旧函数** `_calc_dream_trigger_threshold`（L560-584）。

### Task 2：改造 `_maybe_trigger_nap` 统一使用压缩游标

**文件**：`agent/runner.py` L920-977

**关键改动：触发判断从 dream 游标改为 compress 游标**

当前代码用 dream 游标后的增量消息数轮数做触发判断。改为用 compress 游标后的增量消息数轮数，与阈值计算使用同一个游标，消除游标不同步问题。

**具体改动**：
1. 读取压缩游标 `~/.niu/last_compress.json` 的 `last_compress_id`（用 `_read_cursor_locked`）
2. 从 `db_messages` 中截取压缩游标之后的消息 `post_compress_msgs`（复用现有 dream 游标截取模式：for 循环找 cursor_idx，`db_messages[cursor_idx + 1:]`；游标找不到时 fallback 到全量 `db_messages`）
3. `turn_count = sum(1 for m in post_compress_msgs if role == 'user')`（压缩游标后的 user 消息数）
4. 调 `self._recalc_msg_stats(post_compress_msgs)` 获取 `post_compress_tokens`
5. `threshold = _calc_dream_trigger_threshold_dynamic(context_window, post_compress_msgs, post_compress_tokens)`
6. 触发判断：`if turn_count < threshold: return`
7. 日志：`[Nap] Triggering nap: {turn_count} turns >= threshold {threshold} (avg={avg:.0f} tokens/turn, budget={budget:.0f}, post_compress={len(post_compress_msgs)} msgs)`
8. 删除旧调用 `threshold = _calc_dream_trigger_threshold(context_window)`（L954）
9. `_read_context_window_tokens` 的 import 保留（仍需要读取 context_window）

**注意**：`_maybe_trigger_nap` 中原有的 dream 游标读取逻辑（L929-930）可以删除——触发判断不再需要 dream 游标。dream 游标只在 `_run_nap_background` 中使用（后台执行时读 dream 游标确定增量范围）。

**游标失效处理**：compress 游标指向的消息不在 DB 中时（被压缩删除），fallback 到全量 `db_messages`，与现有 dream 游标截取行为一致（L944）。此时 avg 会包含压缩摘要消息，但这是边界情况，且摘要消息通常只有 1 条，对 avg 影响有限。

### Task 3：build_subagent_system_segments + call_subagent 加 fallback

**文件**：`agent/subagent.py`

**问题**：`build_subagent_system_segments` 在 `call_subagent` 中位于所有 try/except 之前（L763），构建失败直接抛异常拖垮整个子 Agent。

**修复**（两层保护）：

1. **call_subagent 层**（L763）：给整个 `build_subagent_system_segments` 调用加 try/except
```python
try:
    static_system, dynamic_system = build_subagent_system_segments(agent_name)
except Exception:
    static_system = get_subagent_prompt(agent_name) or "You are a helpful assistant."
    dynamic_system = ""
```

2. **build_subagent_system_segments 内部**（L527-529）：给 dynamic_system 构建加 try/except
```python
try:
    from datetime import datetime
    dynamic_system = f"\n\nCurrent Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}" + _brain_region_section
except Exception:
    dynamic_system = _brain_region_section
```

call_subagent 层保护覆盖所有构建步骤（static + dynamic），build_subagent_system_segments 内部保护覆盖 dynamic_system 的 datetime 调用。两层保护确保任何构建失败都有 fallback。

### Task 4：更新测试

**文件**：`tests/test_dream_trigger.py`

新函数是模块级函数，可以直接 import 调用。测试用 `types.SimpleNamespace(role='user', content='x')` 构造 mock 消息对象，配合 token 列表。

全部重写为动态算法测试：

- `test_dynamic_threshold_200k`：200K 窗口 + 19 条 user 消息 + token 列表（每条约 3000）→ threshold ≈ 15-20
- `test_dynamic_threshold_small_window`：32K 窗口 + 同样消息 → threshold 更小（受下限 10 兜底）
- `test_dynamic_threshold_min_samples`：轮数 < 3 → 直接返回 10
- `test_dynamic_threshold_floor_ceiling`：avg 极高→下限 10，avg 极低→上限 50
- `test_dynamic_threshold_tool_heavy`：工具调用密集（avg 大）→ threshold 偏小

### Task 5：更新文档

**文件**：`docs/SYSTEM_MANUAL.md` L189-190

更新小憩模式触发条件描述：
- 旧：`entity-extractor：由睡眠模式（auto-tidy 管线）+ 小憩模式（`_on_turn_end` 按对话轮数）双重触发`
- 新：`entity-extractor：由睡眠模式（auto-tidy 管线）+ 小憩模式（`_on_turn_end` 按压缩后增量对话轮数动态阈值）双重触发`

## 验收标准

1. `ast.parse` 语法检查通过
2. `tests/test_dream_trigger.py` 全部通过
3. 200K 窗口 + 19 轮用户消息（用 `_recalc_msg_stats` 口径计算 token），算出 threshold 在 10-50 区间
4. `build_subagent_system_segments` 和 `call_subagent` 都有 fallback
5. 旧函数 `_calc_dream_trigger_threshold` 完全删除，无残留引用（`grep -rn '_calc_dream_trigger_threshold' agent/ niu_api/ tests/` 无结果）
6. 阈值计算和触发判断统一使用 compress 游标，无游标不同步问题

## 不在范围内

- 睡眠模式（tidy 管道）触发逻辑不动
- 强制压缩（force）触发逻辑不动
- 30% 比例和上下限 10/50 是经验值，后续可调参
- tidy 模式二清空 compress 游标后 avg 包含摘要消息的边界情况（影响有限，已知限制）
- compress 游标读取与 db_messages 读取之间非原子的并发问题（风险低，已知限制）
