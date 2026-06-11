# 上下文配置迁移：从 preferences.json 到 user-config.json

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 contextWindowSize、warningThreshold、targetThreshold、sleepTriggerMinutes 从 `~/.niu/preferences.json` 迁移到 `config/user-config.json`，并补齐三项遗漏：系统手册说明、配置模板、设置窗口字段。

**Why:** `config/user-config.json` 是唯一的大模型配置文件，程序启动必须初始化。`~/.niu/preferences.json` 是用户数据文件，首次运行时可能不存在。contextWindowSize 跟模型强相关（128K/200K/256K/1M），必须在 LLM 配置文件里，否则程序读不到就用默认值，设置窗口也无法配置。

**Architecture:** Python 侧的 `_read_context_window_tokens()` / `_read_context_threshold()` 改为读取 `config/user-config.json` 的 `context` 段。Rust 侧 `load_context_config()` 改为读取同一文件。设置窗口增加 context 配置字段。系统手册增加说明。MCP config-manager 的 `load_user_config()` 默认结构增加 `context` 段。

**Tech Stack:** Python（agent/subagent.py, niu_api/compat.py, mcp-servers/config-manager）+ Rust（launcher）+ Electron（settings UI）

---

## 审计发现（修订计划的基础）

对全代码库做了大模型配置使用点审计，关键发现：

1. **`agent/subagent.py` 是 context 配置的唯一权威读取点** — 修改它即可影响所有消费方（agent_loop.py, context_manager.py, runner.py, compat.py 都是间接调用）
2. **`protectRecentCount` 在 `compat.py` 中绕过 subagent.py 直接读 preferences.json**（2处）— 计划中已覆盖
3. **`sleepTriggerMinutes` 只在 Rust 端读取，Python 端无调用方** — 不需要加 Python 端读取函数
4. **Rust `_context_config` 是死代码**（加载后未传递给 Python）— 注意但不影响迁移
5. **遗漏：`mcp-servers/config-manager` 的 `load_user_config()` 默认结构没有 `context` 段** — 需补充
6. **遗漏：`niu_api/config.py` 的 `Config.load()` 没有 `context` 段** — 如果 Config 类需要暴露 context 配置则需补充
7. **遗漏：`niu_api/llm_proxy.py` 和 `niu_api/chat.py` 读取 user-config.json 但只读 llm/lightrag_llm 段** — 不影响迁移，它们不关心 context 段
8. **设置窗口保存时丢失 `lightrag_llm` 段是预先存在的 bug** — 在此次迁移中一并修复
9. **`tests/conftest.py` 的 `mock_config` fixture 使用 `"context_win": 200000`** — 这是旧格式的测试 mock，不走 `_read_context_window_tokens()` 路径，迁移后不影响功能。但字段名 `"context_win"` 与新配置键 `"contextWindowSize"` 不一致，为避免混淆可在本次迁移中一并更新为 `"contextWindowSize"`（如果 fixture 的消费方也需对应更新）

**不需要修改的 preferences.json 读取**：feishu、lightrag、brain_regions、storage/categories 段不属于 LLM 配置，保留在 preferences.json 不变。

---

## 目标状态

**`config/user-config.json` 新结构：**
```json
{
  "llm": { "presetId": "...", "apiKey": "...", "apiBase": "...", "model": "...", "type": "openai", "reasoning_effort": "" },
  "lightrag_llm": { "presetId": "", "apiKey": "", "apiBase": "", "model": "", "type": "openai", "reasoning_effort": "xhigh" },
  "context": {
    "contextWindowSize": 200000,
    "warningThreshold": 0.8,
    "targetThreshold": 0.5,
    "sleepTriggerMinutes": 5
  }
}
```

---

## Task 1: Python 侧 — 更新 subagent.py 读取函数

**Files:**
- Modify: `agent/subagent.py:41-84`

- [ ] **Step 1: 添加 `_get_user_config_path()` 辅助函数**

在 `_read_context_window_tokens()` 之前添加：

```python
def _get_user_config_path() -> Path:
    """Locate config/user-config.json relative to project root."""
    return Path(__file__).parent.parent / "config" / "user-config.json"
```

- [ ] **Step 2: 重写 `_read_context_window_tokens()`**

```python
def _read_context_window_tokens() -> int:
    """Read context window size from config/user-config.json."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        size = config.get("context", {}).get("contextWindowSize", DEFAULT_CONTEXT_WINDOW_SIZE)
        if isinstance(size, (int, float)) and MIN_CONTEXT_WINDOW_SIZE <= size <= MAX_CONTEXT_WINDOW_SIZE:
            return int(size)
        logger.warning(f"Invalid contextWindowSize {size}, using default {DEFAULT_CONTEXT_WINDOW_SIZE}")
    except Exception:
        pass
    return DEFAULT_CONTEXT_WINDOW_SIZE
```

- [ ] **Step 3: 重写 `_read_context_threshold()`**

```python
def _read_context_threshold(key: str, default: float) -> float:
    """Read a context threshold from config/user-config.json."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get(key, default)
        if isinstance(val, (int, float)) and 0.0 < val < 1.0:
            return float(val)
    except Exception:
        pass
    return default
```

- [ ] **Step 4: 添加 `_read_protect_recent_count()` 函数**

消除 compat.py 中两处内联读取 preferences.json 的重复代码：

```python
DEFAULT_PROTECT_RECENT_COUNT = 10

def _read_protect_recent_count() -> int:
    """Read protectRecentCount from config/user-config.json. Default 10."""
    try:
        config_path = _get_user_config_path()
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        val = config.get("context", {}).get("protectRecentCount", DEFAULT_PROTECT_RECENT_COUNT)
        if isinstance(val, int) and val >= 0:
            return val
    except Exception:
        pass
    return DEFAULT_PROTECT_RECENT_COUNT
```

- [ ] **Step 5: 验证 Python 读取正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from agent.subagent import _read_context_window_tokens, _read_warning_threshold, _read_target_threshold, _read_protect_recent_count; print(_read_context_window_tokens(), _read_warning_threshold(), _read_target_threshold(), _read_protect_recent_count())"`
Expected: `200000 0.8 0.5 10`

- [ ] **Step 6: Commit**

```bash
git add agent/subagent.py
git commit -m "refactor: context config readers now read from user-config.json instead of preferences.json"
```

---

## Task 2: Python 侧 — 消除 compat.py 中的内联 preferences.json 读取

**Files:**
- Modify: `niu_api/compat.py:16,1192-1198,1671-1677`

- [ ] **Step 1: 更新 import 添加 `_read_protect_recent_count`**

```python
# 替换前:
from agent.subagent import _read_context_window_tokens, _read_target_threshold

# 替换后:
from agent.subagent import _read_context_window_tokens, _read_target_threshold, _read_protect_recent_count
```

- [ ] **Step 2: 替换第一处内联读取（约第 1192-1198 行，sleep 模式）**

```python
# 替换前:
protect_recent_count = 10
try:
    _prefs_path = Path.home() / ".niu" / "preferences.json"
    if _prefs_path.exists():
        _prefs = json.loads(_prefs_path.read_text(encoding="utf-8"))
        protect_recent_count = _prefs.get("context", {}).get("protectRecentCount", 10)
except Exception:
    pass

# 替换后:
protect_recent_count = _read_protect_recent_count()
```

- [ ] **Step 3: 替换第二处内联读取（约第 1671-1677 行，force 模式）**

```python
# 替换前:
protect_recent_count = 10
try:
    from pathlib import Path as _P2
    _prefs2 = json.loads((_P2.home() / ".niu" / "preferences.json").read_text(encoding="utf-8"))
    protect_recent_count = _prefs2.get("context", {}).get("protectRecentCount", 10)
except Exception:
    pass

# 替换后:
protect_recent_count = _read_protect_recent_count()
```

- [ ] **Step 4: 验证 import 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_api.compat import _tidy_context_impl; print('import ok')"`
Expected: `import ok`

- [ ] **Step 5: Commit**

```bash
git add niu_api/compat.py
git commit -m "refactor: compat.py uses _read_protect_recent_count() instead of inline preferences.json reads"
```

---

## Task 3: Rust 侧 — 更新 load_context_config() 读取 user-config.json

**Files:**
- Modify: `launcher/src/main.rs:190-255,699-733`

**关键问题**：`load_context_config()` 当前在第 700 行调用，但 `project_root` 在第 709 行才计算。必须重新排序。

- [ ] **Step 1: 重命名 Preferences → UserConfig，修改读取路径**

```rust
// 替换前 (约第 190-194 行):
/// Helper struct for deserializing preferences.json
#[derive(Debug, Deserialize)]
struct Preferences {
    context: Option<ContextConfigOverrides>,
}

// 替换后:
/// Helper struct for deserializing user-config.json
#[derive(Debug, Deserialize)]
struct UserConfig {
    context: Option<ContextConfigOverrides>,
}
```

注意：Rust 的 serde 默认忽略未知字段，所以 `UserConfig` 只需包含 `context` 字段，`llm`/`lightrag_llm` 等字段不需要声明。

- [ ] **Step 2: 重写 `load_context_config()` 接受 `project_root` 参数**

完整函数体（不可省略）：

```rust
/// LoadContextConfig loads context config from config/user-config.json
fn load_context_config(project_root: &str) -> ContextConfig {
    let mut cfg = default_context_config();

    let config_path = PathBuf::from(project_root).join("config").join("user-config.json");
    let data = match fs::read_to_string(&config_path) {
        Ok(d) => d,
        Err(_) => {
            info!("user-config.json not found at {}, using default context config", config_path.display());
            return cfg;
        }
    };

    let user_config: UserConfig = match serde_json::from_str(&data) {
        Ok(c) => c,
        Err(e) => {
            warn!("Failed to parse user-config.json: {}, using default context config", e);
            return cfg;
        }
    };

    if let Some(ctx) = user_config.context {
        if let Some(v) = ctx.warning_threshold {
            if v > 0.0 && v < 1.0 {
                cfg.warning_threshold = v;
            } else {
                warn!("Invalid warningThreshold {}, must be between 0 and 1, using default {}", v, cfg.warning_threshold);
            }
        }
        if let Some(v) = ctx.target_threshold {
            if v > 0.0 && v < 1.0 {
                cfg.target_threshold = v;
            } else {
                warn!("Invalid targetThreshold {}, must be between 0 and 1, using default {}", v, cfg.target_threshold);
            }
        }
        if let Some(v) = ctx.sleep_trigger_minutes {
            if v > 0 {
                cfg.sleep_trigger_minutes = v;
            }
        }
        if let Some(v) = ctx.context_window_size {
            let vi = v as i32;
            if v > 0.0 && vi >= 32000 && vi <= 2_000_000 {
                cfg.context_window_size = vi;
            } else {
                warn!("Invalid contextWindowSize {}, using default {}", v, cfg.context_window_size);
            }
        }
    }

    cfg
}
```

- [ ] **Step 3: 调整代码顺序 — 将 project_root 计算移到 load_context_config 之前**

将第 709-733 行的 `project_root` 计算代码块整体移到第 700 行之前。`project_root` 计算只依赖 `env::current_exe()` 和 `env::current_dir()`，不依赖 `load_context_config()` 结果，移动安全。后续 `init_niu_dir(&project_root)` 和 `api_server_cmd.current_dir(&project_root_bg)` 调用不受影响。

```rust
// 替换前:
let _context_config = load_context_config();
// ... python detection ...
let exe_path = env::current_exe()...;
// ... project_root computation ...
init_niu_dir(&project_root);

// 替换后:
// 1. 先计算 project_root
let exe_path = env::current_exe()...;
// ... project_root computation ...
// 2. 再加载 context 配置
let _context_config = load_context_config(&project_root);
// 3. 初始化用户目录
init_niu_dir(&project_root);
```

- [ ] **Step 4: 验证 Rust 编译**

Run: `cd REDACTED_USER_PATH/tools/ai-bot/launcher && cargo build --release 2>&1 | tail -3`
Expected: `Finished release profile`

- [ ] **Step 5: Commit**

```bash
git add launcher/src/main.rs
git commit -m "refactor: Rust load_context_config() reads from config/user-config.json"
```

---

## Task 4: 设置窗口 — 增加 context 配置字段

**Files:**
- Modify: `ui/settings/index.html`
- Modify: `ui/settings/main.js` (如有必要)

- [ ] **Step 1: 在 index.html 中增加上下文配置字段**

contextWindowSize 与模型选择密切相关，放在主卡片中（API Key 之后）。其余阈值和 sleepTriggerMinutes 在高级选项中。

**主卡片**：在 API Key 的 `</div>` 之后（约第 234 行之后）、主卡片 `</div>` 关闭标签之前，添加：

```html
<div class="form-group" style="margin-top: 12px;">
  <label class="pencil-text">上下文窗口大小 (tokens)</label>
  <input type="number" id="contextWindowSize" placeholder="200000" min="32000" max="2000000" step="1000">
  <div style="font-size: 12px; color: #666; margin-top: 4px;">模型上下文窗口：128K=128000, 200K=200000, 256K=256000, 1M=1000000</div>
</div>
```

**高级选项**：在 `advancedFields` 中，模型名称输入框之后，添加阈值配置卡片：

```html
<div class="note-card mint" style="margin-top: 15px;">
  <div class="form-group">
    <label class="pencil-text">溢出警告阈值</label>
    <input type="number" id="warningThreshold" placeholder="0.8" min="0.1" max="0.99" step="0.05">
    <div style="font-size: 12px; color: #666; margin-top: 4px;">上下文使用率达到此比例时触发压缩（默认 0.8 = 80%）</div>
  </div>

  <div class="form-group" style="margin-top: 12px;">
    <label class="pencil-text">压缩目标阈值</label>
    <input type="number" id="targetThreshold" placeholder="0.5" min="0.1" max="0.99" step="0.05">
    <div style="font-size: 12px; color: #666; margin-top: 4px;">强制压缩时目标使用率（默认 0.5 = 50%）</div>
  </div>

  <div class="form-group" style="margin-top: 12px;">
    <label class="pencil-text">睡眠触发时间 (分钟)</label>
    <input type="number" id="sleepTriggerMinutes" placeholder="5" min="1" max="60" step="1">
    <div style="font-size: 12px; color: #666; margin-top: 4px;">空闲多久后触发睡眠整理（默认 5 分钟）</div>
  </div>
</div>
```

- [ ] **Step 2: 在 init() 中加载 context 配置（约第 289 行）**

在 `if (config.llm) { ... }` 块的 `}` 之后添加：

```javascript
if (config.context) {
  document.getElementById('contextWindowSize').value = config.context.contextWindowSize || '';
  document.getElementById('warningThreshold').value = config.context.warningThreshold || '';
  document.getElementById('targetThreshold').value = config.context.targetThreshold || '';
  document.getElementById('sleepTriggerMinutes').value = config.context.sleepTriggerMinutes || '';
}
```

- [ ] **Step 3: 在 testAndSave() 中读取现有配置并保存 context 段（约第 307-341 行）**

**关键**：当前保存逻辑会丢失 `lightrag_llm` 段、`llm.reasoning_effort` 和 `storage` 段（预先存在的 bug），必须一并修复。

```javascript
// 在 testAndSave() 函数开头，const presetId = ... 之前添加:
const existingConfig = await window.electronAPI.getConfig();

// 替换约第 337-341 行的 config 对象:
const config = {
  llm: { presetId, apiKey, apiBase, model, type, reasoning_effort: existingConfig.llm?.reasoning_effort || "" },
  lightrag_llm: existingConfig.lightrag_llm || { presetId: "", apiKey: "", apiBase: "", model: "", type: "openai", reasoning_effort: "xhigh" },
  context: {
    contextWindowSize: parseInt(document.getElementById('contextWindowSize').value) || 200000,
    warningThreshold: parseFloat(document.getElementById('warningThreshold').value) || 0.8,
    targetThreshold: parseFloat(document.getElementById('targetThreshold').value) || 0.5,
    sleepTriggerMinutes: parseInt(document.getElementById('sleepTriggerMinutes').value) || 5
  },
  storage: existingConfig.storage || {},
  firstRun: false
};
```

- [ ] **Step 4: Commit**

```bash
git add ui/settings/index.html ui/settings/main.js
git commit -m "feat: add context config fields to settings window + fix lightrag_llm preservation bug"
```

---

## Task 5: 配置模板 — 更新文件

**Files:**
- Modify: `config/user-config.json` — 增加 context 段
- Modify: `memory/preferences.json` — 删除 context 段

- [ ] **Step 1: 在 config/user-config.json 增加 context 段**

**重要：不要替换整个文件！只追加 context 段，保留所有现有内容（包括 API Key）。**

在现有 `llm` 和 `lightrag_llm` 段之后，添加 `context` 段：

```json
{
  "llm": { ... },
  "lightrag_llm": { ... },
  "context": {
    "contextWindowSize": 200000,
    "warningThreshold": 0.8,
    "targetThreshold": 0.5,
    "sleepTriggerMinutes": 5
  }
}
```

- [ ] **Step 2: 从 memory/preferences.json 模板中删除 context 段**

删除 `preferences.json` 模板中的 `"context": { ... }` 部分。context 配置已迁移到 `user-config.json`，模板不再需要。

- [ ] **Step 3: Commit**

```bash
git add config/user-config.json memory/preferences.json
git commit -m "refactor: add context section to user-config.json template, remove from preferences.json template"
```

---

## Task 5.5: MCP config-manager — 更新默认结构和 complete_setup

**Files:**
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:369-384,821-824`

**审计发现**：config-manager 的 `load_user_config()` 在文件不存在时返回默认结构，其中没有 `context` 段。`complete_setup()` 也没有写入 `context` 段。如果不更新，通过 MCP 工具 `set_llm_config` 保存配置时会丢失 `context` 段。

- [ ] **Step 1: 在 `load_user_config()` 默认结构中增加 `context` 段**

```python
# 替换前 (约第 369-384 行):
def load_user_config() -> dict[str, Any]:
    """Load user configuration."""
    if USER_CONFIG_PATH.exists():
        return json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "llm": {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
            "reasoning_effort": "",
        },
        "storage": {"documentRoot": "", "databasePath": ""},
        "firstRun": True,
    }

# 替换后:
def load_user_config() -> dict[str, Any]:
    """Load user configuration."""
    if USER_CONFIG_PATH.exists():
        return json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "llm": {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
            "reasoning_effort": "",
        },
        "lightrag_llm": {
            "presetId": "",
            "apiKey": "",
            "apiBase": "",
            "model": "",
            "type": "openai",
            "reasoning_effort": "xhigh",
        },
        "context": {
            "contextWindowSize": 200000,
            "warningThreshold": 0.8,
            "targetThreshold": 0.5,
            "sleepTriggerMinutes": 5,
        },
        "storage": {"documentRoot": "", "databasePath": ""},
        "firstRun": True,
    }
```

- [ ] **Step 2: 在 `complete_setup()` 中写入默认 context 段**

在 `complete_setup()` 函数末尾，`save_user_config(config)` 之前，确保 context 段存在：

```python
# 在 "config["firstRun"] = False" 之后添加:
if "context" not in config:
    config["context"] = {
        "contextWindowSize": 200000,
        "warningThreshold": 0.8,
        "targetThreshold": 0.5,
        "sleepTriggerMinutes": 5,
    }
```

- [ ] **Step 3: 验证 import 正常**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python -c "from niu_config_manager import load_user_config; print(load_user_config())"`
Expected: 返回包含 `context` 段的默认结构

- [ ] **Step 4: Commit**

```bash
git add mcp-servers/config-manager/src/niu_config_manager/__init__.py
git commit -m "feat: config-manager default structure includes context section"
```

---

## Task 6: 系统手册 — 增加 context 配置说明

**Files:**
- Modify: `docs/manual-user-guide.md`

**此项对应你提出的第一项遗漏：主 Agent 不知道有这个参数可用。** 手册会被主 Agent 读取，因此手册中的说明直接影响 Agent 是否知道如何配置。

- [ ] **Step 1: 在 1.2 节 LLM 配置之后，1.3 节知识图谱之前，插入新的"1.3 上下文配置"章节**

原 1.3 知识图谱 → 改为 1.4，后续章节号顺延。

```markdown
### 1.3 上下文配置

**配置文件**：`config/user-config.json` 中的 `context` 段

```json
{
  "context": {
    "contextWindowSize": 200000,
    "warningThreshold": 0.8,
    "targetThreshold": 0.5,
    "sleepTriggerMinutes": 5
  }
}
```

**字段说明**：

| 字段 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `contextWindowSize` | 模型上下文窗口大小（tokens） | 200000 | 32000 ~ 2000000 |
| `warningThreshold` | 溢出警告阈值，上下文使用率超过此值触发压缩 | 0.8 | 0.0 ~ 1.0 |
| `targetThreshold` | 强制压缩目标，压缩后上下文使用率降至此值 | 0.5 | 0.0 ~ 1.0 |
| `sleepTriggerMinutes` | 空闲多久后触发睡眠整理（分钟） | 5 | > 0 |

**常见模型的 contextWindowSize**：

| 模型 | 上下文窗口 | 配置值 |
|------|-----------|--------|
| GPT-4o-mini | 128K | 128000 |
| GPT-4o | 128K | 128000 |
| Claude 3.5 Sonnet | 200K | 200000 |
| DeepSeek V3 (deepseek-chat) | 64K | 64000 |
| DeepSeek R1 (deepseek-reasoner) | 128K | 128000 |
| Qwen2.5-Turbo | 1M | 1000000 |
| 本地 Ollama 模型 | 取决于模型 | 按实际配置 |

> **注意**：`contextWindowSize` 与模型强相关，切换模型后需同步更新此值。设置窗口的"高级选项"中可直接配置。

**修改配置方式**：
- **方式一（推荐）**：通过设置窗口修改（首次启动自动弹出，点"高级选项"展开）
- **方式二**：关闭程序后，手动编辑 `config/user-config.json`
```

- [ ] **Step 2: 更新后续章节编号**

原 1.3 → 1.4，1.4 → 1.5，1.5 → 1.6，1.6 → 1.7，1.7 → 1.8

- [ ] **Step 3: Commit**

```bash
git add docs/manual-user-guide.md
git commit -m "docs: add context configuration section to user guide"
```

---

## Task 7: 验证 — 全链路测试

**Files:**
- No code changes, verification only

- [ ] **Step 1: 验证 Python 侧从 user-config.json 读取**

Run:
```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "
from agent.subagent import _read_context_window_tokens, _read_warning_threshold, _read_target_threshold, _read_protect_recent_count
print(f'contextWindowSize: {_read_context_window_tokens()} (expect 200000)')
print(f'warningThreshold: {_read_warning_threshold()} (expect 0.8)')
print(f'targetThreshold: {_read_target_threshold()} (expect 0.5)')
print(f'protectRecentCount: {_read_protect_recent_count()} (expect 10)')
"
```
Expected: 所有值与配置一致

- [ ] **Step 2: 验证非法值防护**

在 `config/user-config.json` 中设置非法值：
- `contextWindowSize: 0` → 应返回 200000
- `contextWindowSize: 5000000` → 应返回 200000
- `warningThreshold: 1.5` → 应返回 0.8

- [ ] **Step 3: 验证文件不存在时回退到默认值**

临时移走 `config/user-config.json`，运行读取函数，确认返回默认值，然后恢复。

- [ ] **Step 4: 验证 Rust 编译**

Run: `cd REDACTED_USER_PATH/tools/ai-bot/launcher && cargo build --release 2>&1 | tail -3`
Expected: `Finished release profile`

- [ ] **Step 5: 验证 compat.py 不再读取 preferences.json 的 context 段**

Run: `grep -n "preferences.json" niu_api/compat.py`
Expected: 不应包含 context/warningThreshold/targetThreshold/protectRecentCount 相关的 preferences.json 读取。其他用途（feishu/lightrag/brain_regions）的引用可以保留。

- [ ] **Step 6: 升级兼容性验证**

模拟已有用户场景：`config/user-config.json` 没有 `context` 段，确认读取函数返回默认值：
```bash
# 临时移走 context 段，运行读取函数，确认返回默认值
python -c "
from agent.subagent import _read_context_window_tokens, _read_warning_threshold
print(f'Without context section: contextWindowSize={_read_context_window_tokens()}, warningThreshold={_read_warning_threshold()}')
"
```
Expected: `contextWindowSize=200000, warningThreshold=0.8`（默认值）

- [ ] **Step 7: 手动验证设置窗口**

1. 启动程序，打开设置窗口（`--settings` 或首次运行）
2. 点击"高级选项"展开
3. 确认 context 字段正确显示当前值
4. 修改 contextWindowSize 为 128000
5. 保存
6. 检查 `config/user-config.json` 是否包含完整 context 段
7. 确认 `lightrag_llm` 段未被丢失
8. 确认 `llm.reasoning_effort` 未被丢失

- [ ] **Step 8: 恢复 config/user-config.json 为正确值并提交**

```bash
git commit --allow-empty -m "verify: context config migration from preferences.json to user-config.json works correctly"
```

---

## 执行顺序

```
Task 1 (subagent.py)  ──>  Task 2 (compat.py)
Task 3 (main.rs)        （独立）
Task 5 (config templates) ──>  Task 4 (settings UI)  ← Task 5 先执行，确保设置窗口加载时有 context 段可显示
Task 5.5 (config-manager) （独立）
Task 6 (manual)         （独立）
Task 7 (验证)           ──>  所有上述 Task
```

Task 1 和 2 必须按顺序。Task 5 必须在 Task 4 之前（确保 user-config.json 已有 context 段，设置窗口加载时能显示当前值）。Task 3、5.5、6 可并行。Task 7 最后。

**注意**：`protectRecentCount` 暂不在设置窗口中暴露（高级参数，普通用户不需要调整），用户可通过手动编辑 `config/user-config.json` 配置。

**向后兼容说明**：已有用户如果手动修改了 `~/.niu/preferences.json` 中的 context 值（如 contextWindowSize=128000），升级后这些自定义值不会被自动迁移到 `config/user-config.json`。读取函数会回退到默认值 200000。用户需在升级后通过设置窗口或手动编辑 `config/user-config.json` 重新配置。此行为应在 release notes 中说明。

---

## 风险评估

| 风险 | 严重性 | 缓解措施 |
|------|----------|----------|
| `user-config.json` 首次运行不存在 | 低 | 读取函数已处理 FileNotFoundError，返回默认值。设置窗口首次保存时创建文件。 |
| Rust `load_context_config()` 调用顺序 | 中 | 必须将 `project_root` 计算移到 `load_context_config()` 之前 |
| 设置 UI 保存时丢失 `lightrag_llm` | 中 | 保存时先读取现有配置，合并后再写入。这是此迁移中一并修复的预先存在的 bug |
| config-manager `set_llm_config` 保存时丢失 `context` 段 | 中 | `load_user_config()` 默认结构增加 context 段，`complete_setup()` 确保写入 context 段 |
| `protectRecentCount` 以前未在 user-config.json 中 | 低 | 默认值 10，与之前行为一致。首次通过设置窗口保存时自动写入 |
| `_get_user_config_path()` 路径解析在某些部署环境下可能不正确 | 中 | 使用 `Path(__file__).parent.parent / "config"` 相对路径，与 `niu_api/config.py` 一致。Rust 侧使用 `project_root` + "config/user-config.json" |
