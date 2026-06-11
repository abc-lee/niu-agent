# 上下文窗口大小可配置化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将硬编码的 200K 上下文窗口大小改为从配置文件读取，使其能适配不同模型（128K/200K/256K/1M），仅主模型需要此配置，LightRAG 模型不需要。同时将 Python 侧的 warningThreshold 从硬编码 0.85 改为从配置读取（默认 0.80，与 Rust 对齐），targetThreshold 也从配置读取。

**行为变更**: warningThreshold 从 0.85 改为 0.80 后，溢出检测会更早触发（80% vs 之前 85%），压缩会更早开始。这是为了与 Rust 启动器保持一致，且 80% 是更合理的压缩触发点。

**Architecture:** 统一通过 `_read_context_window_tokens()` 读取上下文窗口大小，消除所有硬编码 200000。新增 `_read_warning_threshold()` 和 `_read_target_threshold()` 读取百分比阈值，使 Python 侧与 Rust 启动器行为一致。阈值读取函数在循环外调用一次，避免重复磁盘 I/O。

**Tech Stack:** Python（agent/niu_api）+ Rust（launcher）

---

## 现状分析

### 硬编码 200000 的位置（必须修复）

| 文件 | 行号 | 当前代码 | 问题 |
|------|------|----------|------|
| `agent/subagent.py` | 49 | `return 200000` | fallback 硬编码 |
| `agent/subagent.py` | 52 | `return 200000` | 异常时硬编码 |
| `agent/context_manager.py` | 26 | `max_tokens=200000` | 默认参数硬编码 |
| `niu_api/compat.py` | 853 | `context_window_tokens = 200000` | 初始赋值硬编码 |
| `niu_api/compat.py` | 860 | `.get("contextWindowSize", 200000)` | 默认值硬编码 |
| `tests/test_snowball_e2e.py` | 71 | `if full_tokens > 200_000:` | 测试中硬编码 |
| `launcher/src/main.rs` | 186 | `context_window_size: 200_000` | Rust 启动器默认值（会被配置覆盖，但应统一常量） |

### Python 侧百分比阈值硬编码（与 Rust 侧不一致）

| Python 侧 | 值 | Rust 侧配置 | 差异 |
|-----------|-----|-------------|------|
| `agent_loop.py:185` 溢出检测 | `0.85` | `warningThreshold: 0.80` | **Python 用 0.85，Rust 用 0.80，应统一为 0.80** |
| `compat.py:1558` force 压缩目标 | `0.5` | `targetThreshold: 0.50` | 一致，但 Python 未读取配置 |
| `context_manager.py:118` 压缩判断 | `0.8` | `warningThreshold: 0.80` | 一致，但 Python 未读取配置 |
| `subagent.py:380` FIFO 截断 | `0.75` | 无对应配置 | Python 独有 |

**问题**：`~/.niu/preferences.json` 中已有 `warningThreshold` 和 `targetThreshold` 字段，Rust 启动器已读取使用，但 Python 侧完全忽略这些配置，导致修改配置后 Rust 侧行为变化但 Python 侧不变。

### 配置读取链

```
~/.niu/preferences.json → context 字段
    ↓ 读取
agent/subagent.py::_read_context_window_tokens()  (唯一读取入口)
    ↓ 调用方1: 主Agent
agent/runner.py:1026  →  agent_loop.py → 溢出检测
    ↓ 调用方2: 子Agent
agent/subagent.py:380  →  子 Agent FIFO 截断
    ↓ 调用方3: tidy 函数（独立内联读取，未复用函数）
niu_api/compat.py:853-860
```

### 不在修改范围内的位置

| 文件 | 行号 | 说明 |
|------|------|------|
| `tests/conftest.py` | 36 | 测试 fixture mock 值。测试场景固定使用 200K，不随配置变化，确保测试稳定性。如需测试其他窗口大小，应在具体测试用例中单独 mock |
| `tests/test_subagent_overflow.py` | 65 | 测试 mock 值，保持不变 |
| `agent/generic/agent_loop.py:422-440` | FIFO 截断阈值 0.75 | 无对应配置字段，保持硬编码 |
| `context_manager.py:142` | `keep_count = int(len(messages) * 0.8)` | 压缩保留比率（保留80%消息），语义与触发阈值不同，保持硬编码 |
| `compat.py:1098` | `if usage_percent >= 50:` | journal-agent 触发条件，语义与 targetThreshold 不同（50%是"何时运行日志"而非"压缩到多少"），保持硬编码 |

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `agent/subagent.py` | 修改 | 提取常量 + 增加阈值读取函数 + 配置值校验 |
| `agent/context_manager.py` | 修改 | 默认参数改为从配置读取，触发阈值从配置读取 |
| `niu_api/compat.py` | 修改 | 复用配置读取函数，消除内联硬编码，force 压缩目标从配置读取 |
| `agent/generic/agent_loop.py` | 修改 | 溢出检测阈值从配置读取（循环外读一次） |
| `tests/test_snowball_e2e.py` | 修改 | 消除硬编码 200_000 |
| `launcher/src/main.rs` | 修改 | 配置值范围校验（替换原有 `if v > 0`） |

---

### Task 1: 统一 `_read_context_window_tokens()` 并增加配置值校验

**Files:**
- Modify: `agent/subagent.py:36-52`

- [ ] **Step 1: 在 subagent.py 顶部定义常量并增强 `_read_context_window_tokens()`**

```python
# agent/subagent.py 顶部（import 区域后）
DEFAULT_CONTEXT_WINDOW_SIZE = 200000
MIN_CONTEXT_WINDOW_SIZE = 32000    # 32K 最小合理值
MAX_CONTEXT_WINDOW_SIZE = 2000000  # 2M 上限
```

修改 `_read_context_window_tokens()`：

```python
def _read_context_window_tokens() -> int:
    """Read context window size from ~/.niu/preferences.json."""
    try:
        home = Path.home()
        prefs_path = home / ".niu" / "preferences.json"
        with open(prefs_path, "r") as f:
            prefs = json.load(f)
        size = prefs.get("context", {}).get("contextWindowSize", DEFAULT_CONTEXT_WINDOW_SIZE)
        if isinstance(size, (int, float)) and MIN_CONTEXT_WINDOW_SIZE <= size <= MAX_CONTEXT_WINDOW_SIZE:
            return int(size)
        logger.warning(f"Invalid contextWindowSize {size}, using default {DEFAULT_CONTEXT_WINDOW_SIZE}")
    except Exception:
        pass
    return DEFAULT_CONTEXT_WINDOW_SIZE
```

- [ ] **Step 2: 增加 `_read_context_threshold()` 函数**

在同一文件中增加百分比阈值读取函数，默认值与 Rust 启动器对齐（warningThreshold=0.80，targetThreshold=0.50）：

```python
def _read_context_threshold(key: str, default: float) -> float:
    """Read a context threshold from ~/.niu/preferences.json.
    
    Args:
        key: Field name in context section (e.g. 'warningThreshold', 'targetThreshold')
        default: Default value if key not found or invalid
    """
    try:
        home = Path.home()
        prefs_path = home / ".niu" / "preferences.json"
        with open(prefs_path, "r") as f:
            prefs = json.load(f)
        val = prefs.get("context", {}).get(key, default)
        if isinstance(val, (int, float)) and 0.0 < val < 1.0:
            return float(val)
    except Exception:
        pass
    return default

def _read_warning_threshold() -> float:
    """Read warning threshold (overflow detection). Default 0.80, matching Rust launcher."""
    return _read_context_threshold("warningThreshold", 0.80)

def _read_target_threshold() -> float:
    """Read target threshold (force compress target). Default 0.50, matching Rust launcher."""
    return _read_context_threshold("targetThreshold", 0.50)
```

- [ ] **Step 3: 验证修改不影响现有功能**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.subagent import _read_context_window_tokens, _read_warning_threshold, _read_target_threshold; print(_read_context_window_tokens(), _read_warning_threshold(), _read_target_threshold())"`
Expected: `200000 0.8 0.5`

- [ ] **Step 4: Commit**

```bash
git add agent/subagent.py
git commit -m "refactor: extract context config constants + add threshold readers in subagent.py"
```

---

### Task 2: 消除 `niu_api/compat.py` 中的硬编码

**Files:**
- Modify: `niu_api/compat.py:853-860,1558`

已验证：`niu_api/compat.py` 当前已有 `from agent.session import get_message_store`，说明 `niu_api -> agent` 的导入方向已存在，新增 `from agent.subagent import ...` 不会引入循环依赖。

- [ ] **Step 1: 在 compat.py 中导入并复用配置读取函数**

在 compat.py 的 import 区域添加：

```python
from agent.subagent import _read_context_window_tokens, _read_target_threshold
```

将第 853-860 行的内联读取替换为：

```python
# 替换前（约第 853-860 行）:
context_window_tokens = 200000
try:
    home = str(Path.home())
    prefs_path = os.path.join(home, ".niu", "preferences.json")
    with open(prefs_path, "r") as f:
        prefs = json.load(f)
    context_window_tokens = prefs.get("context", {}).get("contextWindowSize", 200000)
except Exception:
    pass

# 替换后:
context_window_tokens = _read_context_window_tokens()
```

- [ ] **Step 2: 将 force 压缩目标中的硬编码 0.5 改为从配置读取**

找到 force 压缩目标中使用 `0.5` 的地方（约第 1558 行）：

```python
# 替换前:
target_tokens = int(estimated_tokens * 0.5)

# 替换后:
target_tokens = int(estimated_tokens * _read_target_threshold())
```

注意：`compat.py:1098` 的 `if usage_percent >= 50:` 不修改。journal-agent 触发条件的 50% 与 targetThreshold 语义不同（前者是"何时运行日志"，后者是"压缩到多少"），保持硬编码。

- [ ] **Step 3: 验证修改不影响 tidy 功能**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_api.compat import _tidy_context_impl; print('import ok')"`
Expected: `import ok`

- [ ] **Step 4: Commit**

```bash
git add niu_api/compat.py
git commit -m "refactor: reuse context config readers in compat.py, remove hardcoded 200000 and 0.5"
```

---

### Task 3: 消除 `agent/context_manager.py` 中的硬编码

**Files:**
- Modify: `agent/context_manager.py:26,118`

已验证：`context_manager.py` 只导入 `agent.session`，不导入 `agent.subagent`。`agent.subagent` 也不导入 `agent.context_manager`。所以 `context_manager.py -> agent.subagent` 的导入不存在循环风险，无需延迟导入。

- [ ] **Step 1: 修改 ContextManager 的默认参数和触发阈值**

```python
# 在文件顶部 import 区域添加:
from agent.subagent import _read_context_window_tokens, _read_warning_threshold

# 替换 __init__ 默认参数（约第 26 行）:
# 替换前:
def __init__(self, max_tokens=200000, ...):
# 替换后:
def __init__(self, max_tokens=None, ...):
    if max_tokens is None:
        max_tokens = _read_context_window_tokens()
    self._warning_threshold = _read_warning_threshold()

# 替换 should_compress 触发阈值（约第 118 行）:
# 替换前:
if tokens > self.max_tokens * 0.8:
# 替换后:
if tokens > self.max_tokens * self._warning_threshold:
```

注意：`compress_messages()` 中的 `keep_count = int(len(messages) * 0.8)`（约第 142 行）不修改。这是压缩保留比率（保留80%消息），语义与触发阈值不同，保持硬编码。

- [ ] **Step 2: 验证无循环导入**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.context_manager import ContextManager; cm = ContextManager(); print(cm.max_tokens)"`
Expected: `200000`

- [ ] **Step 3: Commit**

```bash
git add agent/context_manager.py
git commit -m "refactor: remove hardcoded 200000 and 0.8 from ContextManager, use config readers"
```

---

### Task 4: `agent_loop.py` 溢出检测阈值从配置读取

**Files:**
- Modify: `agent/generic/agent_loop.py:185`

**关键设计**：`_read_warning_threshold()` 必须在 while 循环外读取一次并存为局部变量，避免每次循环迭代都读磁盘。

- [ ] **Step 1: 在循环外读取阈值，循环内使用局部变量**

```python
# 在文件顶部 import 区域添加:
from agent.subagent import _read_warning_threshold

# 在 agent_runner_loop 函数内，while 循环之前（约第 175 行附近，与 context_window_tokens 一起）:
warning_threshold = _read_warning_threshold()

# 替换溢出检测（约第 185 行）:
# 替换前:
if usage_ratio > 0.85:
# 替换后:
if usage_ratio > warning_threshold:
```

- [ ] **Step 2: 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.generic.agent_loop import agent_runner_loop; print('import ok')"`
Expected: `import ok`

- [ ] **Step 3: Commit**

```bash
git add agent/generic/agent_loop.py
git commit -m "refactor: agent_loop overflow threshold reads from config (0.80) instead of hardcoded 0.85"
```

---

### Task 5: 消除 `tests/test_snowball_e2e.py` 中的硬编码

**Files:**
- Modify: `tests/test_snowball_e2e.py:71`

- [ ] **Step 1: 替换测试中的硬编码 200_000**

```python
# 替换前:
if full_tokens > 200_000:
    print(f"  ⚠️  prompt tokens ({full_tokens:,}) 超过 200K 窗口！子 Agent 会溢出")

# 替换后:
from agent.subagent import _read_context_window_tokens
context_window = _read_context_window_tokens()
if full_tokens > context_window:
    print(f"  ⚠️  prompt tokens ({full_tokens:,}) 超过 {context_window // 1000}K 窗口！子 Agent 会溢出")
```

- [ ] **Step 2: 验证测试可导入**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.subagent import _read_context_window_tokens; print(_read_context_window_tokens())"`
Expected: `200000`

- [ ] **Step 3: Commit**

```bash
git add tests/test_snowball_e2e.py
git commit -m "refactor: test uses _read_context_window_tokens() instead of hardcoded 200_000"
```

---

### Task 6: 统一 Rust 启动器的配置值校验

**Files:**
- Modify: `launcher/src/main.rs:243`

Rust 启动器的 `load_context_config()` 已从 `preferences.json` 读取 `contextWindowSize`。当前校验是 `if v > 0`（第 243 行），允许任何正值（包括不合理的 1）。需要替换为范围校验，与 Python 侧对齐。

- [ ] **Step 1: 替换 Rust 侧的配置值校验**

```rust
// 替换前（约第 242-245 行）:
if v > 0 {
    cfg.context_window_size = v;
}

// 替换后:
if v >= 32000 && v <= 2_000_000 {
    cfg.context_window_size = v;
} else {
    warn!("Invalid contextWindowSize {}, using default {}", v, cfg.context_window_size);
}
```

- [ ] **Step 2: 验证 Rust 编译**

Run: `cd REDACTED_USER_PATH/tools/ai-bot/launcher && cargo build --release 2>&1 | tail -3`
Expected: `Finished release profile`

- [ ] **Step 3: Commit**

```bash
git add launcher/src/main.rs
git commit -m "refactor: add contextWindowSize range validation (32K-2M) in Rust launcher"
```

---

### Task 7: 验证全链路 — 修改配置值后所有读取点一致

**Files:**
- No code changes, verification only

- [ ] **Step 1: 修改配置值并验证**

修改 `~/.niu/preferences.json`：

```json
{
  "context": {
    "contextWindowSize": 128000,
    "warningThreshold": 0.8,
    "targetThreshold": 0.5
  }
}
```

验证脚本：

```bash
python -c "
from agent.subagent import _read_context_window_tokens, _read_warning_threshold, _read_target_threshold
from agent.context_manager import ContextManager

print(f'contextWindowSize: {_read_context_window_tokens()} (expect 128000)')
print(f'warningThreshold: {_read_warning_threshold()} (expect 0.8)')
print(f'targetThreshold: {_read_target_threshold()} (expect 0.5)')

cm = ContextManager()
print(f'ContextManager.max_tokens: {cm.max_tokens} (expect 128000)')

print(f'All consistent: {_read_context_window_tokens() == cm.max_tokens == 128000}')
"
```

Expected: 所有值与配置一致

- [ ] **Step 2: 测试非法值防护**

修改 `contextWindowSize` 为非法值验证：
- `0` → 应返回 200000
- `-1` → 应返回 200000
- `"abc"` → 应返回 200000
- `5000000` → 应返回 200000（超过 2M 上限）

- [ ] **Step 3: 恢复原始配置**

将 `preferences.json` 改回原始值。

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "verify: contextWindowSize config read chain + threshold readers work correctly"
```

---

## 自审检查

### Spec 覆盖

| 需求 | 对应 Task |
|------|-----------|
| 消除硬编码 200000 | Task 1 (subagent.py) + Task 2 (compat.py) + Task 3 (context_manager.py) + Task 5 (test) + Task 6 (Rust) |
| 统一从配置读取 | Task 1 (统一入口+校验) + Task 2 (复用函数) |
| 配置值校验 | Task 1 (Python 范围校验) + Task 6 (Rust 范围校验) |
| warningThreshold 从 0.85 改为配置读取（默认 0.80） | Task 1 (读取函数) + Task 3 (context_manager) + Task 4 (agent_loop) |
| targetThreshold 从配置读取 | Task 1 (读取函数) + Task 2 (compat.py) |
| 仅主模型支持 | 已确认：LightRAG 模型无上下文窗口配置，无需改动 |

### 明确排除的位置

| 位置 | 原因 |
|------|------|
| `context_manager.py:142` 保留比率 0.8 | 语义不同于触发阈值，是"压缩时保留多少"，保持硬编码 |
| `compat.py:1098` journal-agent 触发 50% | 语义不同于 targetThreshold，是"何时运行日志"，保持硬编码 |
| `subagent.py:380` FIFO 截断 0.75 | 无对应配置字段，是子 Agent 内部策略，保持硬编码 |

### Placeholder 扫描

无 TBD/TODO/implement later 等占位符。

### 类型一致性

- `_read_context_window_tokens()` 返回 `int`，所有调用方使用 `int`，一致
- `_read_warning_threshold()` / `_read_target_threshold()` 返回 `float`，用于乘法比较，一致
- Rust 侧 `context_window_size` 是 `i32`，Python 侧 `int`，跨语言通过 JSON 数字类型通信，一致
