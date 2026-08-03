# 持久化 EMA 每轮 Token 均值 + 精确 TokenCalculator 集成

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用持久化的指数移动平均（EMA）替换临时计算的 avg_tokens_per_turn，使小憩模式触发阈值随使用时间逐步收敛，冷启动保守（保底 10 轮），长期趋于准确。

**Architecture:** 在 `_on_turn_end` 中每轮用 `TokenCalculator`（DeepSeek tokenizer）精确计算本轮新增消息的 token 数，更新持久化的 EMA 均值（存 `~/.niu/avg_tokens_per_turn.json`）。`_calc_dream_trigger_threshold_dynamic` 改为读持久化 EMA 值算阈值，不再每次从消息列表临时算。冷启动（样本 < 5 轮）直接返回保底 10，随样本增加 EMA 逐步反映真实每轮开销，阈值从 10 逐步偏移到真实值。

**Tech Stack:** Python 3.11, agent/token_calculator.py (DeepSeek-V3 tokenizer), JSON 文件持久化（加文件锁）

---

## 问题背景

当前 `_calc_dream_trigger_threshold_dynamic` 每次调用时从 compress 游标后的消息临时算 avg_tokens_per_turn：
- 用 `_recalc_msg_stats` 算 token（虽然调了 TokenCalculator，但只算消息 content，不含 system prompt、tools schema、历史累积）
- 临时计算的 avg 样本少时不准（压缩后只有几轮消息）
- 没有持久化，每次都重新算，无法积累

实际数据：compress 游标后 92 条消息、26 轮，avg = 654 tokens/轮 → threshold = 50（上限兜底），22 轮 < 50 不触发。但真实 LLM prompt_tokens 每轮约 3700，threshold 应该约 16 才对。

## 算法设计：指数移动平均（EMA）+ 冷启动保护

### 为什么用 EMA

- **简单平均**（sum/count）：早期的高值/低值会拖偏均值，需要"遗忘"旧数据
- **滑动窗口**：需要存储 N 轮的历史数据，复杂
- **EMA**：只需存一个值（当前 EMA），每轮用 `EMA = α × new_sample + (1-α) × EMA`，自动衰减旧数据

### EMA 公式（非对称衰减 — 张力模型）

像弹簧张力：拉紧费力（上升慢），松手弹回快（下降快）。

```
α_up = 0.2    # 上升慢：新样本占 20%，历史占 80%
α_down = 0.5  # 下降快：新样本占 50%，历史占 50%

if current_avg > ema_old:
    EMA_new = α_up × current_avg + (1 - α_up) × ema_old    # 慢上升
else:
    EMA_new = α_down × current_avg + (1 - α_down) × ema_old  # 快下降
```

- 上升（连续高开销对话）：α=0.2，EMA 缓慢爬升，threshold 缓慢下降（拉紧费力）
- 下降（几轮短对话）：α=0.5，EMA 快速回落，threshold 快速回到 10（松手弹回）
- 非对称设计避免短暂低开销对话误拉低 threshold，同时高开销趋势能持续累积

### 冷启动保护

- 样本数 < 5 时：不更新 EMA，`_calc_dream_trigger_threshold_dynamic` 直接返回保底 10
- 样本数 >= 5 时：EMA 已积累足够样本，开始用它算阈值
- 首次启动（无持久化文件）：EMA 初始值 = 0，样本数 = 0，走冷启动保护

### 持久化格式

文件：`~/.niu/avg_tokens_per_turn.json`
```json
{
  "ema": 3500.0,
  "sample_count": 42,
  "last_updated_at": "2026-08-03T14:30:00.123456"
}
```

用 `_write_cursor_with_lock` / `_read_cursor_locked` 模式读写（加文件锁）。

### 阈值计算

```python
# 读持久化 EMA
ema, sample_count = read_ema_from_file()

# 冷启动保护
if sample_count < 5:
    return 10  # 保底

avg_tokens_per_turn = max(1000, ema)  # 安全下限
incremental_budget = context_window * 0.30
threshold = int(incremental_budget / avg_tokens_per_turn)
return max(10, min(50, threshold))
```

### 200K 窗口验证

- 冷启动（sample_count < 5）：threshold = 10
- EMA 收敛到 3700（真实每轮开销）：threshold = int(60000 / 3700) = 16
- EMA 收敛到 6000（工具密集）：threshold = int(60000 / 6000) = 10（下限兜底）
- EMA 收敛到 1500（纯文本短对话）：threshold = int(60000 / 1500) = 40

### 每轮新增 token 的计算口径

关键问题："本轮新增消息"的 token 怎么算？

**方案：用 agent_loop 的 messages 列表差量**

`_on_turn_end(messages, tools_schema, turn)` 被调用时，`messages` 是完整的消息列表（含历史 + 本轮新增）。`turn` 是 agent_loop 的当前轮次（从 1 开始）。

但 agent_loop 的一个"turn"可能包含多轮 LLM 调用（工具调用循环），不是用户的"一轮对话"。我们需要的是"用户一轮对话（user 消息 + 所有 assistant/tool 回复）的 token 总量"。

**实际做法**：在 `_on_turn_end` 中，记录上次调用时的 messages 长度（`self._last_msg_count`），本轮新增的 messages = `messages[self._last_msg_count:]`，用 `TokenCalculator.count_messages()` 算 token。但这只在 agent_loop 结束时调用一次（不是每轮工具调用），需要确认。

**更简单的方案**：不在 `_on_turn_end` 中逐轮累加，而是在 `_maybe_trigger_nap` 中一次性算 compress 游标后所有消息的 token（用 `TokenCalculator.count_messages()`），除以轮数，更新 EMA。这样不需要改 `_on_turn_end` 的签名。

**最终选择：在 `_maybe_trigger_nap` 中更新 EMA**

`_maybe_trigger_nap` 已经从 DB 读 `post_compress_msgs`，已经用 `_recalc_msg_stats` 算了 token。只需：
1. 改用 `TokenCalculator.count_messages()` 替代 `_recalc_msg_stats`（更精确，包含结构开销）
2. 算 avg = total_tokens / turn_count
3. 用 EMA 公式更新持久化文件
4. `_calc_dream_trigger_threshold_dynamic` 读持久化 EMA 算阈值

---

## File Structure

| 文件 | 改动 | 职责 |
|------|------|------|
| `agent/runner.py` | 修改 | 新增 `_read_ema` / `_write_ema` 方法；改造 `_maybe_trigger_nap` 更新 EMA；改造 `_calc_dream_trigger_threshold_dynamic` 读 EMA |
| `tests/test_dream_trigger.py` | 修改 | 重写测试，mock EMA 文件 |
| `docs/SYSTEM_MANUAL.md` | 修改 | 更新算法描述 |

---

### Task 1: 新增 EMA 读写方法

**Files:**
- Modify: `agent/runner.py`（NiuRunner 类中，`_read_cursor_locked` 附近新增两个静态方法）
- Test: `tests/test_dream_trigger.py`

- [ ] **Step 1: 写 EMA 读写方法的测试**

```python
import json
import os
import tempfile
from unittest.mock import patch
from pathlib import Path


class TestEMAReadWrite:
    """测试 EMA 读写持久化方法。"""

    def test_read_ema_no_file(self, tmp_path):
        """文件不存在时返回 (0.0, 0)。"""
        from agent.runner import NiuRunner
        ema, count = NiuRunner._read_ema(tmp_path / "nonexistent.json")
        assert ema == 0.0
        assert count == 0

    def test_write_then_read_ema(self, tmp_path):
        """写入后读取应一致。"""
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        NiuRunner._write_ema(path, ema=3500.0, sample_count=10)
        ema, count = NiuRunner._read_ema(path)
        assert ema == 3500.0
        assert count == 10

    def test_read_ema_corrupt_file(self, tmp_path):
        """文件损坏时返回 (0.0, 0)。"""
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        path.write_text("corrupt json")
        ema, count = NiuRunner._read_ema(path)
        assert ema == 0.0
        assert count == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py::TestEMAReadWrite -v`
Expected: FAIL — `AttributeError: type object 'NiuRunner' has no attribute '_read_ema'`

- [ ] **Step 3: 实现 `_read_ema` 和 `_write_ema`**

在 `agent/runner.py` 的 NiuRunner 类中，`_read_cursor_locked` 方法之后，新增两个 `@staticmethod`：

```python
@staticmethod
def _read_ema(ema_path):
    """读取持久化的 EMA 值和样本数。

    Returns:
        (ema: float, sample_count: int)，文件不存在或损坏时返回 (0.0, 0)
    """
    if not ema_path.exists():
        return 0.0, 0
    try:
        import json
        from niu_api.compat import _flock, _funlock
        lock_path = ema_path.with_suffix(".lock")
        with open(lock_path, "w") as lock_f:
            _flock(lock_f)
            try:
                data = json.loads(ema_path.read_text(encoding="utf-8"))
                return float(data.get("ema", 0.0)), int(data.get("sample_count", 0))
            finally:
                _funlock(lock_f)
    except Exception as e:
        logger.warning(f"[Nap] Failed to read EMA {ema_path.name}: {e}")
        return 0.0, 0

@staticmethod
def _write_ema(ema_path, ema: float, sample_count: int):
    """写入持久化的 EMA 值和样本数（加文件锁）。"""
    try:
        import json
        from datetime import datetime
        from niu_api.compat import _flock, _funlock
        lock_path = ema_path.with_suffix(".lock")
        with open(lock_path, "w") as lock_f:
            _flock(lock_f)
            try:
                ema_path.write_text(json.dumps({
                    "ema": ema,
                    "sample_count": sample_count,
                    "last_updated_at": datetime.now().isoformat(),
                }, ensure_ascii=False), encoding="utf-8")
            finally:
                _funlock(lock_f)
    except Exception as e:
        logger.warning(f"[Nap] Failed to write EMA {ema_path.name}: {e}")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py::TestEMAReadWrite -v`
Expected: PASS — 3 tests

- [ ] **Step 5: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 6: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py tests/test_dream_trigger.py
git commit -m "feat: add _read_ema/_write_ema for persistent EMA storage"
```

---

### Task 2: 改造 `_calc_dream_trigger_threshold_dynamic` 读持久化 EMA

**Files:**
- Modify: `agent/runner.py:560-608`（`_calc_dream_trigger_threshold_dynamic` 函数）
- Test: `tests/test_dream_trigger.py`

- [ ] **Step 1: 写新函数的测试**

在 `tests/test_dream_trigger.py` 中，新增测试类：

```python
class TestCalcDreamTriggerThresholdEMA:
    """测试改造后的动态阈值函数（读持久化 EMA）。"""

    def test_cold_start_sample_below_5(self, tmp_path):
        """样本数 < 5 时返回保底 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        # 不写文件 → sample_count=0 < 5 → 返回 10
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_ema_3700_200k(self, tmp_path):
        """EMA=3700, 200K 窗口 → threshold=16。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=3700.0, sample_count=10)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 16  # int(60000 / 3700) = 16

    def test_ema_6000_200k_floor(self, tmp_path):
        """EMA=6000, 200K 窗口 → threshold=10（下限兜底）。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=6000.0, sample_count=20)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10  # int(60000 / 6000) = 10, max(10, 10) = 10

    def test_ema_1500_200k_ceiling(self, tmp_path):
        """EMA=1500, 200K 窗口 → threshold=40。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=1500.0, sample_count=15)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 40  # int(60000 / 1500) = 40

    def test_zero_context_window(self, tmp_path):
        """context_window=0 → 返回 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        threshold = _calc_dream_trigger_threshold_dynamic(0, ema_path)
        assert threshold == 10
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py::TestCalcDreamTriggerThresholdEMA -v`
Expected: FAIL — 签名不匹配（旧函数接受 `post_compress_msgs, post_compress_tokens`，新测试传 `ema_path`）

- [ ] **Step 3: 重写 `_calc_dream_trigger_threshold_dynamic`**

替换 `agent/runner.py` 中的 `_calc_dream_trigger_threshold_dynamic` 函数（L560-608）：

```python
def _calc_dream_trigger_threshold_dynamic(
    context_window: int,
    ema_path,
) -> int:
    """根据持久化 EMA 值动态计算 dream-evolver 触发阈值。

    算法：
    - 读持久化的 EMA（指数移动平均每轮 token 开销）
    - 冷启动（样本 < 5）直接返回保底 10
    - avg = max(1000, EMA)
    - threshold = (context_window × 0.30) / avg
    - 下限 10，上限 50

    200K + EMA=3700 → 60000/3700 = 16
    200K + EMA=6000 → 60000/6000 = 10（下限兜底）
    """
    MIN_TURNS = 10
    MAX_TURNS = 50
    MIN_AVG_TOKENS = 1000
    BUDGET_RATIO = 0.30
    MIN_SAMPLES = 5

    if context_window <= 0:
        return MIN_TURNS

    ema, sample_count = NiuRunner._read_ema(ema_path)

    if sample_count < MIN_SAMPLES:
        return MIN_TURNS

    avg_tokens_per_turn = max(MIN_AVG_TOKENS, ema)
    incremental_budget = context_window * BUDGET_RATIO
    threshold = int(incremental_budget / avg_tokens_per_turn)

    return max(MIN_TURNS, min(MAX_TURNS, threshold))
```

- [ ] **Step 4: 运行新测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py::TestCalcDreamTriggerThresholdEMA -v`
Expected: PASS — 5 tests

- [ ] **Step 5: 删除旧的 TestCalcDreamTriggerThresholdDynamic 测试类**

删除 `tests/test_dream_trigger.py` 中旧的 `TestCalcDreamTriggerThresholdDynamic` 类（它用 `SimpleNamespace` mock 消息，现在函数签名变了不再适用）。

- [ ] **Step 6: 运行全部测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py -v`
Expected: PASS — 所有测试

- [ ] **Step 7: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); ast.parse(open('tests/test_dream_trigger.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 8: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py tests/test_dream_trigger.py
git commit -m "refactor: _calc_dream_trigger_threshold_dynamic reads persistent EMA

- 新签名: (context_window, ema_path) -> int
- 读持久化 EMA 替代临时从消息列表算 avg
- 冷启动（样本 < 5）返回保底 10
- 删除旧的 TestCalcDreamTriggerThresholdDynamic 测试类"
```

---

### Task 3: 改造 `_maybe_trigger_nap` 更新 EMA + 调用新函数

**Files:**
- Modify: `agent/runner.py:956-1010`（`_maybe_trigger_nap` 方法）

- [ ] **Step 1: 读取当前 `_maybe_trigger_nap` 代码**

读取 `agent/runner.py` 中 `_maybe_trigger_nap` 方法（搜索 `def _maybe_trigger_nap`），确认当前代码结构。

- [ ] **Step 2: 改造 `_maybe_trigger_nap`**

改造要点：
1. 读 compress 游标后的消息 `post_compress_msgs`（保留现有逻辑）
2. 用 `TokenCalculator.count_messages()` 替代 `_recalc_msg_stats` 算总 token
3. 算 avg = total_tokens / turn_count
4. 读持久化 EMA，用 EMA 公式更新：`EMA_new = 0.3 × avg + 0.7 × EMA_old`
5. 写回持久化 EMA
6. 调用改造后的 `_calc_dream_trigger_threshold_dynamic(context_window, ema_path)`

具体改动（在 `_maybe_trigger_nap` 中）：

```python
# --- 原有代码保留：读游标、读 db_messages、截取 incremental_msgs、算 turn_count ---

# 截取压缩游标后的消息（用于 EMA 更新和阈值计算）
post_compress_msgs = _slice_after_cursor(db_messages, last_compress_id)

# 用 TokenCalculator 精确计算压缩游标后所有消息的 token
from agent.token_calculator import TokenCalculator
calc = TokenCalculator.get()
post_compress_token_total = calc.count_messages(
    [{"role": getattr(m, "role", ""), "content": getattr(m, "content", "") or ""} for m in post_compress_msgs]
)

# 算 avg（每轮 user 消息对应的平均 token）
post_compress_turns = sum(1 for m in post_compress_msgs if getattr(m, "role", "") == "user")
if post_compress_turns > 0:
    current_avg = post_compress_token_total / post_compress_turns
else:
    current_avg = 0

# 更新持久化 EMA
ema_path = niu_dir / "avg_tokens_per_turn.json"
ema_old, sample_count = self._read_ema(ema_path)
ALPHA_UP = 0.2    # 上升慢（拉紧费力）
ALPHA_DOWN = 0.5  # 下降快（松手弹回）
MIN_SAMPLES = 5

if sample_count < MIN_SAMPLES:
    # 冷启动期：直接用当前 avg 初始化（不做加权平均）
    new_ema = current_avg
    new_sample_count = sample_count + 1
elif current_avg > ema_old:
    # 上升：慢速更新
    new_ema = ALPHA_UP * current_avg + (1 - ALPHA_UP) * ema_old
    new_sample_count = sample_count + 1
else:
    # 下降：快速回落
    new_ema = ALPHA_DOWN * current_avg + (1 - ALPHA_DOWN) * ema_old
    new_sample_count = sample_count + 1

if current_avg > 0:
    self._write_ema(ema_path, new_ema, new_sample_count)

# 计算阈值
from agent.subagent import _read_context_window_tokens
context_window = _read_context_window_tokens()
threshold = _calc_dream_trigger_threshold_dynamic(context_window, ema_path)

# 日志
logger.info(f"[Nap] turn_count={turn_count}, threshold={threshold}, EMA={new_ema:.0f}, samples={new_sample_count}")

if turn_count < threshold:
    return
```

注意：
- 保留 `incremental_msgs` 和 `turn_count` 的现有逻辑（dream 游标后，用于触发判断）
- `post_compress_msgs` 和 `post_compress_turns` 是 compress 游标后（用于 EMA 更新）
- 两个游标各司其职：dream 游标防重复触发，compress 游标保证 EMA 样本质量
- 删除旧的 `post_compress_tokens = self._recalc_msg_stats(post_compress_msgs)` 调用
- 删除旧的 `threshold = _calc_dream_trigger_threshold_dynamic(context_window, post_compress_msgs, post_compress_tokens)` 调用
- 日志格式更新为包含 EMA 值和样本数

- [ ] **Step 3: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 4: 运行定向测试**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py -v`
Expected: PASS — 所有测试

- [ ] **Step 5: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/runner.py
git commit -m "feat: _maybe_trigger_nap updates persistent EMA + calls new threshold function

- 用 TokenCalculator.count_messages() 替代 _recalc_msg_stats
- 算 compress 游标后消息 avg token，用 EMA 公式更新持久化值
- 冷启动期直接用当前 avg 初始化
- 调用改造后的 _calc_dream_trigger_threshold_dynamic(context_window, ema_path)
- 日志增加 EMA 值和样本数"
```

---

### Task 4: 更新文档

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md` L382-383

- [ ] **Step 1: 读取当前文档**

读取 `docs/SYSTEM_MANUAL.md` L380-385，确认当前文本。

- [ ] **Step 2: 更新 L382-383**

更新 L382（触发阈值行）：
- 旧：`| 触发阈值 | \`_calc_dream_trigger_threshold_dynamic(context_window, post_compress_msgs, post_compress_tokens)\`，下限 10 轮，上限 50 轮 |`
- 新：`| 触发阈值 | \`_calc_dream_trigger_threshold_dynamic(context_window, ema_path)\`，下限 10 轮，上限 50 轮 |`

更新 L383（阈值算法行）：
- 旧：`| 阈值算法 | 轮数<3时直接返回10；否则 max(10, min(50, int(context_window × 0.30 / max(1000, avg_tokens_per_turn))))，avg 基于压缩游标后消息动态计算 |`
- 新：`| 阈值算法 | 冷启动(样本<5)返回10；否则 max(10, min(50, int(context_window × 0.30 / max(1000, EMA))))，EMA 为持久化的指数移动平均每轮 token 开销（α=0.3，用 TokenCalculator 精确计算） |`

- [ ] **Step 3: Commit**

```bash
cd /Users/lilei/tools/ai-bot
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: update SYSTEM_MANUAL for persistent EMA algorithm"
```

---

## 验收标准

1. `ast.parse` 语法检查通过（runner.py + test_dream_trigger.py）
2. `tests/test_dream_trigger.py` 全部通过
3. 冷启动（sample_count < 5）返回 10
4. EMA=3700 + 200K 窗口 → threshold=16
5. EMA=6000 + 200K 窗口 → threshold=10（下限兜底）
6. EMA 持久化到 `~/.niu/avg_tokens_per_turn.json`，加文件锁
7. 旧函数 `_calc_dream_trigger_threshold` 无残留引用（`grep -rw` 无结果）
8. `_maybe_trigger_nap` 用 `TokenCalculator.count_messages()` 算 token

## 不在范围内

- 睡眠模式（tidy 管道）触发逻辑不动
- 强制压缩（force）触发逻辑不动
- α=0.3 和上下限 10/50 是经验值，后续可调参
- EMA 文件的并发 TOCTOU 问题（风险低，已知限制）
