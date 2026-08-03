# 持久化 EMA 每轮 Token 均值 + 精确 TokenCalculator 集成

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用持久化的指数移动平均（EMA）替换临时计算的 avg_tokens_per_turn，使小憩模式触发阈值随使用时间逐步收敛，冷启动保守（保底 10 轮），长期趋于准确。

**Architecture:** 在 `_maybe_trigger_nap` 中用 `TokenCalculator`（DeepSeek tokenizer）精确计算 compress 游标后所有消息的 token，算 avg，用非对称 EMA（张力模型）更新持久化均值。`_calc_dream_trigger_threshold_dynamic` 改为读持久化 EMA 算阈值。冷启动（样本 < 5 轮）直接返回保底 10。增加去重机制避免 agent_loop 多轮工具调用重复更新 EMA。

**Tech Stack:** Python 3.11, agent/token_calculator.py (DeepSeek-V3 tokenizer), JSON 文件持久化（加文件锁）

---

## 问题背景

当前 `_calc_dream_trigger_threshold_dynamic` 每次调用时从 compress 游标后的消息临时算 avg_tokens_per_turn，用 `_recalc_msg_stats` 算 token。问题：
- 临时算的 avg 样本少时不准（压缩后只有几轮消息）
- 没有持久化，每次都重新算，无法积累
- 实际数据：avg = 654 tokens/轮 → threshold = 50（上限兜底），22 轮 < 50 不触发

## 算法设计：非对称 EMA + 冷启动保护

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

- 样本数 < 5 时：`_calc_dream_trigger_threshold_dynamic` 直接返回保底 10
- 冷启动期（sample_count < 5 或 ema_old=0）：每次更新都用 current_avg 覆盖 EMA（非累积均值，有意设计——冷启动期样本少不可靠，sample_count 达到 5 后开始用 EMA 公式累积）
- 样本数 >= 5 时：开始用非对称 EMA 公式更新
- 防御：ema_old=0 时（异常状态），直接用 current_avg 初始化

### 去重机制（关键设计）

解决：记录上次调用时的 `post_compress_turns`（compress 游标后的 user 消息数），只在它增加时才更新 EMA。这样一次 agent_loop 会话中只更新一次 EMA。压缩发生后 post_compress_turns 会变小（游标前移），此时重置 `_last_ema_turns = 0` 让 EMA 从新的 post_compress_turns 重新开始去重计数。

### 持久化格式

文件：`~/.niu/avg_tokens_per_turn.json`
```json
{
  "ema": 3500.0,
  "sample_count": 42,
  "last_updated_at": "2026-08-03T14:30:00.123456"
}
```

### 200K 窗口验证

- 冷启动（sample_count < 5）：threshold = 10
- EMA 收敛到 3700：threshold = int(60000 / 3700) = 16
- EMA 收敛到 6000：threshold = int(60000 / 6000) = 10（下限兜底）
- EMA 收敛到 1500：threshold = int(60000 / 1500) = 40

### current_avg 计算口径

在 `_maybe_trigger_nap` 中一次性算 compress 游标后所有消息的 token（用 `TokenCalculator.count_messages()`），除以 user 消息数。注意：(1) 转换 db_messages 为 dict 时必须包含 `tool_calls` 字段；(2) current_avg 是 compress 游标后的累积平均（总 token / 总轮数），不是单轮增量。EMA 跟踪的是这个累积平均的变化趋势——随着轮数增加 current_avg 趋于稳定，EMA 的非对称张力模型主要在跨 compress 周期间起作用（压缩后 current_avg 重新开始计算）。

---

## File Structure

| 文件 | 改动 | 职责 |
|------|------|------|
| `agent/runner.py` | 修改 | 新增 `_read_ema` / `_write_ema` 方法；改造 `_maybe_trigger_nap` 更新 EMA（含去重）；改造 `_calc_dream_trigger_threshold_dynamic` 读 EMA |
| `tests/test_dream_trigger.py` | 修改 | 重写测试，mock EMA 文件，覆盖上升/下降分支 |
| `docs/SYSTEM_MANUAL.md` | 修改 | 更新算法描述 |

---

### Task 1: 新增 EMA 读写方法

**Files:**
- Modify: `agent/runner.py`（NiuRunner 类中，`_read_cursor_locked` 附近新增两个静态方法）
- Test: `tests/test_dream_trigger.py`

- [ ] **Step 1: 写 EMA 读写方法的测试**

```python
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

    def test_read_ema_missing_fields(self, tmp_path):
        """文件存在但 ema/sample_count 字段缺失时返回默认值。"""
        from agent.runner import NiuRunner
        path = tmp_path / "avg.json"
        path.write_text('{"other": "data"}')
        ema, count = NiuRunner._read_ema(path)
        assert ema == 0.0
        assert count == 0

    def test_write_ema_creates_parent_dir(self, tmp_path):
        """_write_ema 应创建不存在的父目录。"""
        from agent.runner import NiuRunner
        path = tmp_path / "subdir" / "avg.json"
        NiuRunner._write_ema(path, ema=3500.0, sample_count=10)
        assert path.exists()
        ema, count = NiuRunner._read_ema(path)
        assert ema == 3500.0
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
        ema_path.parent.mkdir(parents=True, exist_ok=True)
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
Expected: PASS — 5 tests

- [ ] **Step 5: 语法检查 + Commit**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax OK')"
git add agent/runner.py tests/test_dream_trigger.py
git commit -m "feat: add _read_ema/_write_ema for persistent EMA storage"
```

---

### Task 2: 改造 `_calc_dream_trigger_threshold_dynamic` 读持久化 EMA

**Files:**
- Modify: `agent/runner.py`（`_calc_dream_trigger_threshold_dynamic` 函数）
- Test: `tests/test_dream_trigger.py`

- [ ] **Step 1: 写新函数的测试**

```python
class TestCalcDreamTriggerThresholdEMA:
    """测试改造后的动态阈值函数（读持久化 EMA）。"""

    def test_cold_start_sample_below_5(self, tmp_path):
        """样本数 < 5 时返回保底 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_ema_3700_200k(self, tmp_path):
        """EMA=3700, 200K 窗口 → threshold=16。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=3700.0, sample_count=10)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 16

    def test_ema_6000_200k_floor(self, tmp_path):
        """EMA=6000, 200K 窗口 → threshold=10（下限兜底）。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=6000.0, sample_count=20)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_ema_1500_200k_threshold_40(self, tmp_path):
        """EMA=1500, 200K 窗口 → threshold=40。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=1500.0, sample_count=15)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 40

    def test_zero_context_window(self, tmp_path):
        """context_window=0 → 返回 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        threshold = _calc_dream_trigger_threshold_dynamic(0, ema_path)
        assert threshold == 10

    def test_negative_context_window(self, tmp_path):
        """context_window=-1 → 返回 10。"""
        from agent.runner import _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        threshold = _calc_dream_trigger_threshold_dynamic(-1, ema_path)
        assert threshold == 10

    def test_sample_count_5_boundary(self, tmp_path):
        """sample_count=5（边界值）→ 使用 EMA 公式。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=3000.0, sample_count=5)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 20  # int(60000 / 3000) = 20

    def test_sample_count_4_cold_start(self, tmp_path):
        """sample_count=4（边界值）→ 冷启动返回 10。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=3000.0, sample_count=4)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 10

    def test_ema_zero_with_samples(self, tmp_path):
        """ema=0 且 sample_count>=5 → max(1000, 0)=1000 → threshold=50。"""
        from agent.runner import NiuRunner, _calc_dream_trigger_threshold_dynamic
        ema_path = tmp_path / "avg.json"
        NiuRunner._write_ema(ema_path, ema=0.0, sample_count=10)
        threshold = _calc_dream_trigger_threshold_dynamic(200000, ema_path)
        assert threshold == 50  # int(60000 / 1000) = 60, min(50, 60) = 50
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py::TestCalcDreamTriggerThresholdEMA -v`
Expected: FAIL — 签名不匹配

- [ ] **Step 3: 重写 `_calc_dream_trigger_threshold_dynamic`**

```python
def _calc_dream_trigger_threshold_dynamic(
    context_window: int,
    ema_path,
) -> int:
    """根据持久化 EMA 值动态计算 dream-evolver 触发阈值。

    算法：
    - 读持久化的 EMA（非对称指数移动平均每轮 token 开销）
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

- [ ] **Step 4: 删除旧的 TestCalcDreamTriggerThresholdDynamic 测试类和死代码**

删除 `tests/test_dream_trigger.py` 中旧的 `TestCalcDreamTriggerThresholdDynamic` 类。同时删除模块级 `from agent.runner import _calc_dream_trigger_threshold_dynamic` 导入、`_make_msgs` 辅助函数和 `from types import SimpleNamespace` 导入（新测试类均不使用）。

- [ ] **Step 5: 运行全部测试 + 语法检查 + Commit**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -m pytest tests/test_dream_trigger.py -v
python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); ast.parse(open('tests/test_dream_trigger.py').read()); print('syntax OK')"
git add agent/runner.py tests/test_dream_trigger.py
git commit -m "refactor: _calc_dream_trigger_threshold_dynamic reads persistent EMA"
```

---

### Task 3: 改造 `_maybe_trigger_nap` 更新 EMA + 调用新函数

**Files:**
- Modify: `agent/runner.py`（`_maybe_trigger_nap` 方法 + `__init__`）

- [ ] **Step 1: 读取当前 `_maybe_trigger_nap` 代码**

读取 `agent/runner.py` 中 `_maybe_trigger_nap` 方法，确认当前代码结构。

- [ ] **Step 2: 在 `__init__` 中初始化去重变量**

在 `self._nap_running = threading.Event()` 附近，新增：

```python
self._last_ema_turns = 0  # 去重：记录上次更新 EMA 时的 post_compress_turns
```

- [ ] **Step 3: 改造 `_maybe_trigger_nap`**

改造要点：
1. 保留原有 dream 游标读取逻辑和 incremental_msgs 截取（用于触发判断）
2. 新增 compress 游标读取和 post_compress_msgs 截取
3. 用 `TokenCalculator.count_messages()` 算 token（转换 dict 时包含 tool_calls）
4. 算 current_avg = total_tokens / post_compress_turns
5. 去重：只在 post_compress_turns > self._last_ema_turns 时更新 EMA
6. 非对称 EMA 更新：冷启动（sample_count < 5 或 ema_old=0）直接初始化，否则用 α_up/α_down
7. 调用改造后的 `_calc_dream_trigger_threshold_dynamic(context_window, ema_path)`

具体改动代码：

```python
# --- 保留原有 dream 游标读取和 incremental_msgs 截取 ---
# --- 保留 turn_count 计算 ---

# 截取压缩游标后的消息（用于 EMA 更新）
post_compress_msgs = _slice_after_cursor(db_messages, last_compress_id)
post_compress_turns = sum(1 for m in post_compress_msgs if getattr(m, "role", "") == "user")

# 压缩后游标前移导致 post_compress_turns 变小，重置去重计数器
if post_compress_turns < self._last_ema_turns:
    self._last_ema_turns = 0
# 已知边界：post_compress_turns == _last_ema_turns 时两个条件均为 False，跳过一次更新。自愈（下次 turn 恢复），概率极低（压缩通常显著减少 turns）。

ema_path = niu_dir / "avg_tokens_per_turn.json"

# 去重 + 更新 EMA：post_compress_turns >= 1 即可（冷启动保护在 threshold 函数中）
if post_compress_turns > self._last_ema_turns and post_compress_turns >= 1:
    self._last_ema_turns = post_compress_turns

    # 用 TokenCalculator 精确计算 token（包含 tool_calls 结构开销）
    from agent.token_calculator import TokenCalculator
    calc = TokenCalculator.get()
    post_compress_dicts = [
        {
            "role": getattr(m, "role", ""),
            "content": getattr(m, "content", "") or "",
            "tool_calls": getattr(m, "tool_calls", []) or [],
        }
        for m in post_compress_msgs
    ]
    post_compress_token_total = calc.count_messages(post_compress_dicts)
    current_avg = post_compress_token_total / post_compress_turns

    ema_old, sample_count = self._read_ema(ema_path)
    ALPHA_UP = 0.2    # 上升慢（拉紧费力）
    ALPHA_DOWN = 0.5  # 下降快（松手弹回）
    MIN_SAMPLES = 5

    if sample_count < MIN_SAMPLES or ema_old == 0:
        # 冷启动期或 EMA 未初始化：直接用 current_avg 初始化
        new_ema = current_avg
    elif current_avg > ema_old:
        # 上升：慢速更新
        new_ema = ALPHA_UP * current_avg + (1 - ALPHA_UP) * ema_old
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
logger.info(f"[Nap] turn_count={turn_count}, threshold={threshold}, post_compress_turns={post_compress_turns}")

if turn_count < threshold:
    return
```

注意：
- 删除旧的 `post_compress_tokens = self._recalc_msg_stats(post_compress_msgs)` 调用（仅删除 _maybe_trigger_nap 中的调用，_recalc_msg_stats 方法本身保留，_run_nap_background 和 force 路径仍使用）
- 删除旧的 `threshold = _calc_dream_trigger_threshold_dynamic(context_window, post_compress_msgs, post_compress_tokens)` 调用
- 保留 `incremental_msgs` 和 `turn_count` 的现有逻辑（dream 游标，用于触发判断）
- `post_compress_turns >= 1` 即可更新 EMA（冷启动保护在 `_calc_dream_trigger_threshold_dynamic` 中通过 `sample_count < 5` 返回 10 实现）

- [ ] **Step 3b: 写 EMA 更新逻辑测试**

在 `tests/test_dream_trigger.py` 中新增测试类，验证非对称 EMA 更新逻辑。将 EMA 更新核心逻辑提取为可测试的独立函数 `_compute_ema_update(ema_old, sample_count, current_avg)`：

```python
class TestEMAUpdateLogic:
    """测试非对称 EMA 更新逻辑。"""

    def test_cold_start_overwrite(self):
        """冷启动期（sample_count < 5）：直接用 current_avg 覆盖。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=0.0, sample_count=0, current_avg=3000.0)
        assert new_ema == 3000.0
        assert new_count == 1

    def test_cold_start_overwrite_at_4(self):
        """sample_count=4 仍走冷启动。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=2500.0, sample_count=4, current_avg=4000.0)
        assert new_ema == 4000.0  # 覆盖，不是 EMA 公式
        assert new_count == 5

    def test_ema_old_zero_overwrite(self):
        """ema_old=0 时直接初始化（即使 sample_count >= 5）。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=0.0, sample_count=10, current_avg=3000.0)
        assert new_ema == 3000.0
        assert new_count == 11

    def test_rising_branch(self):
        """上升分支：current_avg > ema_old → α_up=0.2。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=3000.0, sample_count=10, current_avg=5000.0)
        assert new_ema == 0.2 * 5000.0 + 0.8 * 3000.0  # 3400.0
        assert new_count == 11

    def test_falling_branch(self):
        """下降分支：current_avg <= ema_old → α_down=0.5。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=5000.0, sample_count=10, current_avg=3000.0)
        assert new_ema == 0.5 * 3000.0 + 0.5 * 5000.0  # 4000.0
        assert new_count == 11

    def test_equal_branch(self):
        """current_avg == ema_old → 下降分支（α_down=0.5），结果不变。"""
        from agent.runner import _compute_ema_update
        new_ema, new_count = _compute_ema_update(ema_old=3000.0, sample_count=10, current_avg=3000.0)
        assert new_ema == 3000.0  # 0.5*3000 + 0.5*3000 = 3000
        assert new_count == 11
```

同时需在 `agent/runner.py` 中提取 `_compute_ema_update` 模块级函数：

```python
def _compute_ema_update(ema_old: float, sample_count: int, current_avg: float) -> tuple[float, int]:
    """计算非对称 EMA 更新。返回 (new_ema, new_sample_count)。"""
    ALPHA_UP = 0.2
    ALPHA_DOWN = 0.5
    MIN_SAMPLES = 5

    if sample_count < MIN_SAMPLES or ema_old == 0:
        new_ema = current_avg
    elif current_avg > ema_old:
        new_ema = ALPHA_UP * current_avg + (1 - ALPHA_UP) * ema_old
    else:
        new_ema = ALPHA_DOWN * current_avg + (1 - ALPHA_DOWN) * ema_old

    return new_ema, sample_count + 1
```

在 `_maybe_trigger_nap` 中调用 `_compute_ema_update`（Step 3b 定义的函数）替代 Step 3 中的内联 EMA 逻辑。

运行测试：
```bash
cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_dream_trigger.py::TestEMAUpdateLogic -v
```
Expected: PASS — 6 tests

- [ ] **Step 4: 语法检查 + Commit**

```bash
cd /Users/lilei/tools/ai-bot
python/bin/python -c "import ast; ast.parse(open('agent/runner.py').read()); print('syntax OK')"
git add agent/runner.py
git commit -m "feat: _maybe_trigger_nap updates persistent EMA with asymmetric tension model"
```

---

### Task 4: 更新文档

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md` L382-383

- [ ] **Step 1: 更新 L382-383**

更新 L382（触发阈值行）：
- 旧：`| 触发阈值 | \`_calc_dream_trigger_threshold_dynamic(context_window, post_compress_msgs, post_compress_tokens)\`，下限 10 轮，上限 50 轮 |`
- 新：`| 触发阈值 | \`_calc_dream_trigger_threshold_dynamic(context_window, ema_path)\`，下限 10 轮，上限 50 轮 |`

更新 L383（阈值算法行）：
- 旧：`| 阈值算法 | 轮数<3时直接返回10；否则 max(10, min(50, int(context_window × 0.30 / max(1000, avg_tokens_per_turn))))，avg 基于压缩游标后消息动态计算 |`
- 新：`| 阈值算法 | 冷启动(样本<5)返回10；否则 max(10, min(50, int(context_window × 0.30 / max(1000, EMA))))，EMA 为持久化的非对称指数移动平均每轮 token 开销（上升α=0.2慢、下降α=0.5快，张力模型），用 TokenCalculator 精确计算 |`

- [ ] **Step 2: Commit**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: update SYSTEM_MANUAL for persistent asymmetric EMA algorithm"
```

---

## 验收标准

1. `ast.parse` 语法检查通过（runner.py + test_dream_trigger.py）
2. `tests/test_dream_trigger.py` 全部通过
3. 冷启动（sample_count < 5）返回 10
4. EMA=3700 + 200K 窗口 → threshold=16
5. EMA=6000 + 200K 窗口 → threshold=10（下限兜底）
6. EMA 持久化到 `~/.niu/avg_tokens_per_turn.json`，加文件锁，`_write_ema` 创建父目录
7. `_maybe_trigger_nap` 用 `TokenCalculator.count_messages()` 算 token，转换 dict 包含 tool_calls
8. 去重机制：`_last_ema_turns` 防止 agent_loop 多轮工具调用重复更新 EMA
9. 非对称 EMA：上升 α=0.2，下降 α=0.5
10. `_calc_dream_trigger_threshold_dynamic` 旧签名无残留引用

## 不在范围内

- 睡眠模式（tidy 管道）触发逻辑不动
- 强制压缩（force）触发逻辑不动
- α 值和上下限 10/50 是经验值，后续可调参
- EMA 的 read-modify-write 非原子（_run_nap_background 不操作 EMA 文件，风险低）
