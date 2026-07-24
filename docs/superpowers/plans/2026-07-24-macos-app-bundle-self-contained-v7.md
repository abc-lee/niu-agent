# macOS .app Bundle 自包含实施计划 (v7 — 基于 POC 实测验证)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 让 `niu.app` 真正自包含——所有资源在 bundle 内，Python 自包含不依赖系统 framework，拷到任何 Mac（同 arch）都能跑。

**Architecture:**
- macOS bundle 模式：资源在 `niu.app/Contents/Resources/`，二进制从 `current_exe()` 检测 `.app/Contents/MacOS/` 走 bundle 模式
- macOS dev 模式 / Windows / Linux：资源在 exe 同级目录
- 运行时可写数据在 `~/.niu/`（config/logs/window-config）
- Python 自包含（已 POC 实测验证）：
  - stdlib 复制到 `python/lib/python3.11/`
  - Python dylib 复制到 `python/lib/libPython3.11.dylib`
  - Resources/Python.app stub 复制到 `python/lib/Resources/`
  - install_name_tool 改 dylib 引用为 `@rpath/libPython3.11.dylib` + 加 `@loader_path/../lib` rpath
  - 重签 python3 + dylib
  - 启动器 spawn Python 时传 `PYTHONHOME=<resources_root>/python`

**POC 实测验证结果（2026-07-24）**：
- `import numpy` 1.26.4 ✓
- `import torch` 2.2.2 ✓
- `encodings.__file__` = `python/lib/python3.11/encodings/__init__.py`（bundle 内，不是系统 framework）✓
- 跨目录拷贝到 `/tmp/test_sc2/python` + 改 PYTHONHOME 后 `import numpy/torch` 成功 ✓
- `base_prefix` 指向 bundle 内（PYTHONHOME 生效）✓

**重要约束**：
- ❌ bundle 内文件运行时不可写（config/logs 必须在 ~/.niu/）
- ❌ 不支持跨 arch 分发（torch 2.2.2 macOS wheel 单 arch）
- ❌ codesign --deep 已废弃（macOS 13.3+）
- ❌ install_name_tool 后必须立即重签
- ❌ Python 自包含必须复制 stdlib（不只是 dylib）——这是 v6 plan 的错误教训

---

## Task 1: 新增 `detect_resources_root()` + `detect_niu_home()` + Rust 单元测试

**Files:** `launcher/src/main.rs`

### Step 1: 在 `detect_project_root` 函数上方新增两个函数

```rust
/// Pure function: compute resources root from a given exe path.
fn resources_root_from_exe(exe_path: &std::path::Path) -> PathBuf {
    #[cfg(target_os = "macos")]
    {
        let exe_str = exe_path.to_string_lossy();
        if exe_str.contains(".app/Contents/MacOS/") {
            let contents_dir = exe_path
                .parent()
                .and_then(|p| p.parent())
                .map(|p| p.to_path_buf());
            return match contents_dir {
                Some(contents) => contents.join("Resources"),
                None => PathBuf::from("."),
            };
        }
    }
    exe_path
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Detect the resources root directory.
fn detect_resources_root() -> PathBuf {
    let exe_path = env::current_exe().unwrap_or_else(|_| PathBuf::from("."));
    resources_root_from_exe(&exe_path)
}

/// Detect user data root (~/.niu/).
fn detect_niu_home() -> Result<PathBuf, std::io::Error> {
    let home = dirs::home_dir()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "home_dir not found"))?;
    Ok(home.join(".niu"))
}
```

### Step 2: 文件末尾加 Rust 单元测试（调真实函数 + smoke test）

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resources_root_from_exe_macos_bundle() {
        let exe = std::path::Path::new("/Applications/niu.app/Contents/MacOS/niu");
        let result = resources_root_from_exe(exe);
        assert_eq!(result, PathBuf::from("/Applications/niu.app/Contents/Resources"));
    }

    #[test]
    fn test_resources_root_from_exe_dev_mode() {
        let exe = std::path::Path::new("REDACTED_USER_PATH/tools/ai-bot/niu");
        let result = resources_root_from_exe(exe);
        assert_eq!(result, PathBuf::from("REDACTED_USER_PATH/tools/ai-bot"));
    }

    #[test]
    fn test_detect_niu_home_smoke() {
        let result = detect_niu_home();
        assert!(result.is_ok());
        assert!(result.unwrap().to_string_lossy().ends_with("/.niu"));
    }
}
```

### Step 3: `cargo test --release` 验证全 PASS

### Step 4: 提交

```bash
git add launcher/src/main.rs
git commit -m "feat(launcher): add detect_resources_root + detect_niu_home (Rust #[test])"
```

---

## Task 2: 改造 `detect_project_root` 委托 `detect_resources_root` + 删 cwd fallback

**Files:** `launcher/src/main.rs`（detect_project_root 函数 + main 函数 memory_dir_check 块）

### Step 1: grep 验证 main 函数后续无 exe_path/exe_dir/cwd 引用

```bash
cd REDACTED_USER_PATH/tools/ai-bot
grep -n "fn detect_project_root\|memory_dir_check" launcher/src/main.rs
# 找到 memory_dir_check 块后，看块后是否还有 exe_dir/exe_path/cwd 引用
```

### Step 2: 改 `detect_project_root`

```rust
/// Detect project root directory (= resources root).
fn detect_project_root() -> String {
    detect_resources_root().to_string_lossy().to_string()
}
```

### Step 3: 删除 main 函数 memory_dir_check 块（exe_path + exe_dir + cwd fallback + memory/ 检查），替换为 3 行注释

### Step 4: `./launcher/build.sh` 编译 + `cargo test --release` 测试 PASS

### Step 5: 提交

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): detect_project_root delegates to detect_resources_root"
```

---

## Task 3: 改造 `detect_python` 用 `detect_resources_root` + cargo run cwd fallback + exit code 验证

**Files:** `launcher/src/main.rs`（detect_python 函数）

### Step 1: 改 `detect_python`

```rust
/// Detect project's self-contained Python executable.
fn detect_python() -> String {
    let python_rel_path: PathBuf = if cfg!(target_os = "windows") {
        PathBuf::from("python").join("Scripts").join("python.exe")
    } else {
        PathBuf::from("python").join("bin").join("python3")
    };

    // Primary: resources root
    let resources_root = detect_resources_root();
    let candidate = resources_root.join(&python_rel_path);
    if Command::new(&candidate)
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        let abs_path = dunce::canonicalize(&candidate).unwrap_or_else(|_| candidate.clone());
        info!("Found project Python (resources): {}", abs_path.display());
        return abs_path.to_string_lossy().to_string();
    }

    // Dev fallback: cargo run (cwd=project root)
    let cwd = env::current_dir()
        .map(|d| d.to_string_lossy().to_string())
        .unwrap_or_else(|_| ".".to_string());
    let cwd_candidate = PathBuf::from(&cwd).join(&python_rel_path);
    if Command::new(&cwd_candidate)
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
    {
        let abs_path = dunce::canonicalize(&cwd_candidate).unwrap_or_else(|_| cwd_candidate.clone());
        info!("Found project Python (cwd fallback): {}", abs_path.display());
        return abs_path.to_string_lossy().to_string();
    }

    error!("Project Python not found. Checked resources: {}, cwd: {}", candidate.display(), cwd_candidate.display());
    log_fatal_error(&format!("Project Python not found. Checked resources: {}, cwd: {}", candidate.display(), cwd_candidate.display()));
    std::process::exit(1);
}
```

### Step 2: `./launcher/build.sh` 编译

### Step 3: 提交

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): detect_python uses detect_resources_root + exit code validation"
```

---

## Task 4: 改造 `launch_window` 用 `detect_resources_root`

**Files:** `launcher/src/main.rs`（launch_window 函数）

### Step 1: 改 `launch_window`

```rust
fn launch_window(name: &str) -> Result<std::process::Child, Box<dyn std::error::Error>> {
    let resources_root = detect_resources_root();
    let window_dir = resources_root.join("ui").join("main");

    #[cfg(windows)]
    {
        let mut cmd = Command::new("cmd");
        cmd.args(["/C", "npm", "start"]).env("NIU_WINDOW", name).current_dir(&window_dir);
        cmd.stdout(std::process::Stdio::inherit());
        cmd.stderr(std::process::Stdio::inherit());
        cmd.stdin(std::process::Stdio::inherit());
        Ok(cmd.spawn()?)
    }

    #[cfg(not(windows))]
    {
        let mut cmd = Command::new("npm");
        cmd.arg("start").env("NIU_WINDOW", name).current_dir(&window_dir);
        cmd.stdout(std::process::Stdio::null());
        cmd.stderr(std::process::Stdio::null());
        cmd.stdin(std::process::Stdio::null());
        Ok(cmd.spawn()?)
    }
}
```

### Step 2: `./launcher/build.sh` 编译 + 提交

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): launch_window uses detect_resources_root"
```

---

## Task 5: Rust 侧日志和 config 移到 `~/.niu/`

**Files:** `launcher/src/main.rs`（should_enable_logging + log_fatal_error）

### Step 1: 改 `should_enable_logging` 读 `~/.niu/config/user-config.json`

```rust
fn should_enable_logging() -> bool {
    let config_path = match detect_niu_home() {
        Ok(home) => home.join("config").join("user-config.json"),
        Err(_) => return false,
    };
    match std::fs::read_to_string(&config_path) {
        Ok(content) => match serde_json::from_str::<serde_json::Value>(&content) {
            Ok(v) => v.get("logging").and_then(|l| l.get("enabled")).and_then(|e| e.as_bool()).unwrap_or(false),
            Err(_) => false,
        },
        Err(_) => false,
    }
}
```

### Step 2: 改 `log_fatal_error` 写 `~/.niu/logs/launcher_error.log`（fallback /tmp/）

```rust
fn log_fatal_error(msg: &str) {
    let log_path = match detect_niu_home() {
        Ok(home) => home.join("logs").join("launcher_error.log"),
        Err(_) => PathBuf::from("/tmp/niu_launcher_error.log"),
    };
    let _ = std::fs::create_dir_all(log_path.parent().unwrap_or(std::path::Path::new(".")));
    use time::macros::format_description;
    let format = format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let timestamp = match time::OffsetDateTime::now_local() {
        Ok(t) => t.format(format).unwrap_or_else(|_| "unknown".to_string()),
        Err(_) => "unknown".to_string(),
    };
    let line = format!("[{}] FATAL: {}\n", timestamp, msg);
    let _ = std::fs::OpenOptions::new().create(true).append(true).open(&log_path)
        .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
}
```

### Step 3: `./launcher/build.sh` 编译 + 提交

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): Rust log + config moved to ~/.niu/"
```

---

## Task 6: Python 侧日志路径移到 `~/.niu/logs/`

**Files:** `niu_api/http_log_api.py`, `niu_api/channel/gateway.py`, `agent/generic/http_logger.py`, `agent/generic/litellm_adapter.py`

### Step 1: `niu_api/http_log_api.py` — 新增 `_get_log_dir()` 函数，所有 `_LOG_DIR` 引用改成 `_get_log_dir()` 调用

```python
def _get_log_dir() -> Path:
    """返回日志目录 ~/.niu/logs/raw_http/。"""
    import os
    home = os.path.expanduser("~")
    return Path(home) / ".niu" / "logs" / "raw_http"
```

### Step 2: `niu_api/channel/gateway.py` — `_get_gateway_log_dir` 改返回 `~/.niu/logs/`，L179 飞书 adapter stderr 改用 `_get_gateway_log_dir()`

### Step 3: `agent/generic/http_logger.py` — `_get_log_dir` 改返回 `~/.niu/logs/raw_http/{YYYYMMDD}/`

### Step 4: `agent/generic/litellm_adapter.py` — `_get_app_log_dir` 改返回 `~/.niu/logs/`

### Step 5: Python 语法检查 + 提交

```bash
git add niu_api/http_log_api.py niu_api/channel/gateway.py agent/generic/http_logger.py agent/generic/litellm_adapter.py
git commit -m "refactor: all Python logs write to ~/.niu/logs/"
```

---

## Task 7: Python 侧 config 路径统一到 `~/.niu/config/` + 全仓硬编码扫除

**Files:** `niu_api/config.py`, `niu_api/llm_proxy.py`, `niu_api/chat.py`, `niu_api/compat.py`, `niu_api/internal/lightrag_manager.py` (L222-227 only), `agent/subagent.py`, `agent/mcp_loader.py`, `mcp-servers/config-manager/src/niu_config_manager/__init__.py`

### Step 1: `niu_api/config.py` — 新增 `_get_bundle_config_dir` + `_get_config_path` + `_get_mcp_servers_path`，CONFIG_PATH = _get_config_path()。**删除文件中下部残留的旧 CONFIG_PATH 定义（如果有双定义）**

在 `niu_api/config.py` L45 附近（现有 CONFIG_PATH 定义处）改为：

```python
import os
import shutil
from pathlib import Path

def _get_bundle_config_dir() -> Path:
    """返回 bundle/exe 内的 config 目录（作为模板源）。
    dev 模式: __file__=niu_api/config.py → parent.parent = 项目根 → /config
    bundle 模式: __file__=Contents/Resources/niu_api/config.py → parent.parent = Contents/Resources → /config
    """
    return Path(__file__).resolve().parent.parent / "config"

def _get_config_path() -> str:
    """返回 ~/.niu/config/user-config.json。首次启动从 bundle 内复制模板。"""
    home = os.path.expanduser("~")
    niu_config_dir = Path(home) / ".niu" / "config"
    user_config = niu_config_dir / "user-config.json"
    if not user_config.exists():
        bundle_config = _get_bundle_config_dir() / "user-config.json"
        if bundle_config.exists():
            niu_config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundle_config, user_config)
    return str(user_config)

def _get_mcp_servers_path() -> str:
    """返回 ~/.niu/config/mcp-servers.yaml。首次启动从 bundle 内复制。"""
    home = os.path.expanduser("~")
    niu_config_dir = Path(home) / ".niu" / "config"
    mcp_yaml = niu_config_dir / "mcp-servers.yaml"
    if not mcp_yaml.exists():
        bundle_yaml = _get_bundle_config_dir() / "mcp-servers.yaml"
        if bundle_yaml.exists():
            niu_config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundle_yaml, mcp_yaml)
    return str(mcp_yaml)

CONFIG_PATH = _get_config_path()
```

**重要**：Read 整个 config.py 确认是否有第二处 CONFIG_PATH 定义（v6 执行时发现过 L87 有残留旧定义），有则删除。

### Step 2-4: `niu_api/llm_proxy.py:203` / `niu_api/chat.py:333` / `niu_api/compat.py:1211` 改为 `from niu_api.config import CONFIG_PATH; config_path = Path(CONFIG_PATH)`

### Step 5: `niu_api/internal/lightrag_manager.py` L222-227（**只替换这 6 行，不动 L228+**）

**重要**：`_probe_in_background` 函数从 L220 延伸到 L326（含 httpx 探测、3 次重试、atomic write、异常处理），只替换 L222-227 的 user_config_path 解析块，L228 起的 `with open(user_config_path, encoding="utf-8") as f:` 及之后所有探测逻辑保持不变。

把 L222-227：
```python
            user_config_path = Path.home() / ".niu" / "user-config.json"
            if not user_config_path.exists():
                # 兼容项目内 config/user-config.json
                user_config_path = Path(__file__).parent.parent.parent / "config" / "user-config.json"
            if not user_config_path.exists():
                return
```
替换为：
```python
            from niu_api.config import CONFIG_PATH
            user_config_path = Path(CONFIG_PATH)
            if not user_config_path.exists():
                return
```

Read L218-235 确认实际代码与上述 old_string 一致后再 Edit。

### Step 6: `agent/subagent.py:119` 改为 `from niu_api.config import CONFIG_PATH; return Path(CONFIG_PATH)`

### Step 7: `agent/mcp_loader.py:46` 改为 `from niu_api.config import _get_mcp_servers_path; config_path = Path(_get_mcp_servers_path())`

### Step 7.5: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:338-340` — CONFIG_DIR/USER_CONFIG_PATH 改 `~/.niu/config/`，PRESETS_PATH 保留 bundle 内只读（load_presets 只读不写，os/Path 顶部已导入无需重复 import）

### Step 8: grep 验证全仓无残留硬编码（含 mcp-servers/）

```bash
grep -rn 'Path(__file__).parent.*"config".*user-config' niu_api/ agent/ mcp-servers/ 2>&1 | grep -v "config.py\|_get_bundle" | head -10
```

### Step 9: 验证 config 复制逻辑（rm -rf ~/.niu/config 后跑 python3 -c "from niu_api.config import CONFIG_PATH; print(CONFIG_PATH)"）

### Step 10: 提交（git add 包含 mcp-servers/config-manager）

---

## Task 8: Python API cwd 改成 `~/.niu/` + 启动时传 PYTHONHOME

**Files:** `launcher/src/main.rs`（api_server_cmd.current_dir + env）

### Step 1: 改 api_server_cmd 的 cwd + PYTHONHOME env

```rust
let api_server_cwd = match detect_niu_home() {
    Ok(home) => home.to_string_lossy().to_string(),
    Err(_) => project_root_bg.clone(),
};
api_server_cmd.current_dir(&api_server_cwd);

// PYTHONHOME: 让 python3 找到 bundle 内 stdlib + site-packages
// 必须传（python3 链接的 dylib 内部硬编码系统 framework stdlib 路径，
// PYTHONHOME 覆盖让 base_prefix 指向 bundle 内）
let python_home = PathBuf::from(&python_path_bg)
    .parent()
    .and_then(|p| p.parent())
    .map(|p| p.to_string_lossy().to_string())
    .unwrap_or_default();
api_server_cmd.env("PYTHONHOME", &python_home);
```

**注意**：跟 v6 plan 不同，这里**不需要 pyvenv.cfg 守卫**。已实测验证（2026-07-24）：pyvenv.cfg 的 home 已改成 bundle 内 `python/bin/`，传 PYTHONHOME 后 base_prefix 正确指向 bundle 内 python/，不会泄露到系统 framework。无论 dev 还是 bundle 模式，PYTHONHOME 永远指向 `<resources_root>/python/`，stdlib 在 bundle 内，正确加载。

### Step 2: `./launcher/build.sh` 编译 + 提交

---

## Task 9: Electron 侧 config + window-config 移到 `~/.niu/`

**Files:** `ui/main/main.js`（L36 configPath + L1109 settingsConfigDir）

### Step 1: window-config.json 改到 `~/.niu/window-config.json` + 迁移逻辑

把 L36 附近的 `const configPath = path.join(__dirname, 'windows', 'assistant', 'window-config.json');` 改为：

```javascript
// window-config.json 写到 ~/.niu/（bundle 内只读）
const niuHome = path.join(os.homedir(), '.niu');
if (!fs.existsSync(niuHome)) {
  fs.mkdirSync(niuHome, { recursive: true });
}
const configPath = path.join(niuHome, 'window-config.json');

// 迁移：首次启动若 ~/.niu/window-config.json 不存在但 bundle 内旧路径有文件，复制过去
// 旧路径：ui/main/windows/assistant/window-config.json（升级前用户数据在这里）
const oldConfigPath = path.join(__dirname, 'windows', 'assistant', 'window-config.json');
if (!fs.existsSync(configPath) && fs.existsSync(oldConfigPath)) {
  try {
    fs.copyFileSync(oldConfigPath, configPath);
    console.log('Migrated window-config.json from bundle to ~/.niu/');
  } catch (e) {
    console.error('Failed to migrate window-config.json:', e);
  }
}
```

注意：main.js 顶部可能已有 `const path = require('path')` 和 `const fs = require('fs')`，Read 确认后不重复 require。`os` 可能没 require，需要加 `const os = require('os');`。

### Step 2: settingsConfigDir 改到 `~/.niu/config/`，presetsPath 保留 bundle 内只读

把 L1108-1111 附近：
```javascript
const settingsConfigDir = path.join(__dirname, '..', '..', 'config');
const userConfigPath = path.join(settingsConfigDir, 'user-config.json');
const presetsPath = path.join(settingsConfigDir, 'llm-presets.json');
```
改为：
```javascript
// Config paths：user-config.json 写到 ~/.niu/config/（bundle 内只读）
// llm-presets.json 只读，仍从 bundle 内读
const niuConfigDir = path.join(os.homedir(), '.niu', 'config');
if (!fs.existsSync(niuConfigDir)) {
  fs.mkdirSync(niuConfigDir, { recursive: true });
}
const bundleConfigDir = path.join(__dirname, '..', '..', 'config');
const userConfigPath = path.join(niuConfigDir, 'user-config.json');
const presetsPath = path.join(bundleConfigDir, 'llm-presets.json');  // 只读模板

// 注意：user-config.json 首次启动复制由 Python 侧（niu_api.config._get_config_path）负责。
// Python API 启动比 Electron 早，Electron 启动时 user-config.json 已存在。
// 如果 Electron 启动时仍未存在（极端时序），get-config handler 返回默认 {llm:{}, storage:{}, firstRun:true}，
// 用户通过设置窗口保存时 save-config 会创建文件，不会冲突。
```

### Step 3: `node -c ui/main/main.js` 语法检查 + 提交

---

## Task 10: 写 `scripts/relocate_python_framework.sh`（含复制 stdlib）

**Files:** `scripts/relocate_python_framework.sh`（新建）

### Step 1: 写脚本

```bash
#!/bin/bash
# 把系统 Python.framework 的 stdlib + dylib + Resources stub 复制进 python/，
# 改 python3 + dylib install_name 指向 @rpath/libPython3.11.dylib，
# 重签 python3 + dylib。
#
# 用法：./scripts/relocate_python_framework.sh [python_dir]
set -e

PYTHON_DIR="${1:-./python}"
FRAMEWORK_PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/Python"
FRAMEWORK_LIB="/Library/Frameworks/Python.framework/Versions/3.11/lib/python3.11"
FRAMEWORK_RESOURCES="/Library/Frameworks/Python.framework/Versions/3.11/Resources"

# Step 0: 备份 + otool 留证
cp "$PYTHON_DIR/bin/python3" "$PYTHON_DIR/bin/python3.bak"
otool -L "$PYTHON_DIR/bin/python3" > /tmp/python3_otool_before.txt 2>&1

# Step 1: 复制 stdlib 到 python/lib/python3.11/（排除 site-packages 已有）
echo "[relocate] Step 1: copy stdlib"
rsync -a --exclude='site-packages' "$FRAMEWORK_LIB/" "$PYTHON_DIR/lib/python3.11/"

# Step 2: 复制 Python dylib
echo "[relocate] Step 2: copy Python dylib"
cp "$FRAMEWORK_PYTHON" "$PYTHON_DIR/lib/libPython3.11.dylib"

# Step 3: 复制 Resources/Python.app stub（framework 模式 python3 启动需要）
echo "[relocate] Step 3: copy Resources stub"
cp -R "$FRAMEWORK_RESOURCES" "$PYTHON_DIR/lib/Resources"

# Step 4: 改 dylib install_name (id) 为 @rpath/libPython3.11.dylib
install_name_tool -id @rpath/libPython3.11.dylib "$PYTHON_DIR/lib/libPython3.11.dylib"

# Step 5: 改 python3 二进制的 dylib 引用
install_name_tool -change \
    "$FRAMEWORK_PYTHON" \
    @rpath/libPython3.11.dylib \
    "$PYTHON_DIR/bin/python3"

# Step 6: 加 @loader_path/../lib rpath
install_name_tool -add_rpath @loader_path/../lib "$PYTHON_DIR/bin/python3" 2>/dev/null || true

# Step 7: 重签 python3 + dylib
codesign --force --sign - "$PYTHON_DIR/bin/python3"
codesign --force --sign - "$PYTHON_DIR/lib/libPython3.11.dylib"

# Step 8: 验证（用 PYTHONHOME 模拟启动器行为）
PYTHONHOME_ABS=$(cd "$PYTHON_DIR" && pwd)
echo "--- otool -L python3 ---"
otool -L "$PYTHON_DIR/bin/python3" | grep -i python
echo "--- import numpy/torch ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import numpy; print('numpy', numpy.__version__)"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import torch; print('torch', torch.__version__)"
echo "--- encodings __file__ (should be bundle-internal) ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import encodings; print(encodings.__file__)"
echo "--- sys.prefix / base_prefix ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON_DIR/bin/python3" -c "import sys; print('prefix:', sys.prefix); print('base_prefix:', sys.base_prefix)"

echo "[relocate] DONE"
```

### Step 2: python/ 已是 POC 状态（relocate 已跑过），脚本写完直接跑验证

```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 先恢复 python/ 到 POC 前状态测试脚本（可选，POC 已验证过）
# 或者直接在当前 POC 状态上再跑一次脚本验证幂等性
./scripts/relocate_python_framework.sh python
```

### Step 3: 验证 encodings.__file__ 指向 bundle 内

**关键验证**（审查 Agent 必须跑）：`PYTHONHOME=<bundle>/python python/bin/python3 -c "import encodings; print(encodings.__file__)"` 必须输出 `python/lib/python3.11/encodings/__init__.py`，**不是** `/Library/Frameworks/...`。

### Step 4: 提交脚本（python/ 不进 git，在 .gitignore）

```bash
git add scripts/relocate_python_framework.sh
git commit -m "feat(python): relocate_python_framework.sh (stdlib + dylib + Resources stub)"
```

---

## Task 11: build.sh macOS 分支改造

**Files:** `launcher/build.sh`

### Step 1: 在 macOS 分支 cp niu-launcher 之后、make_icon 之前，插入资源复制

```bash
    RESOURCES_DIR_FULL="$RESOURCES_DIR"
    PROJECT_ROOT_FULL="$(cd .. && pwd)"

    # python/ (含自包含 stdlib + dylib + Resources stub)
    echo "[build.sh] copying python/ to bundle..."
    # 不用 -X：签名完全由 Step 2 codesign --force 重新打，避免 rsync -X 带入旧 xattr（含可能的 quarantine）
    # 排除 *.bak（relocate 脚本的备份文件不进 bundle）
    rsync -a --delete --exclude='*.bak' "$PROJECT_ROOT_FULL/python/" "$RESOURCES_DIR_FULL/python/"
    # 对 bundle 内 python/ 跑 relocate（确保自包含）
    "$PROJECT_ROOT_FULL/scripts/relocate_python_framework.sh" "$RESOURCES_DIR_FULL/python"

    # ui/main/ (Electron)
    echo "[build.sh] copying ui/main/..."
    rsync -a --delete --exclude '.git' --exclude 'node_modules/.cache' \
        "$PROJECT_ROOT_FULL/ui/main/" "$RESOURCES_DIR_FULL/ui/main/"

    # config/ (模板，运行时复制到 ~/.niu/config/)
    echo "[build.sh] copying config/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/config/" "$RESOURCES_DIR_FULL/config/"

    # models/
    echo "[build.sh] copying models/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/models/" "$RESOURCES_DIR_FULL/models/"

    # memory/ (agent templates)
    echo "[build.sh] copying memory/..."
    rsync -a --delete "$PROJECT_ROOT_FULL/memory/" "$RESOURCES_DIR_FULL/memory/" 2>/dev/null || true
```

### Step 2: codesign --deep 改为逐个签名（并行 + 排除 .bak + Electron 主+Helper+Framework glob）

```bash
    echo "[build.sh] signing Python .so + .dylib (parallel)..."
    find ../niu.app/Contents/Resources/python -type f \( -name "*.so" -o -name "*.dylib" \) -not -name "*.bak" -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true

    echo "[build.sh] signing python3 binary..."
    codesign --force --sign - ../niu.app/Contents/Resources/python/bin/python3 2>/dev/null || true

    echo "[build.sh] signing Electron main + Helper + Framework..."
    # Electron 二进制实际路径（实测）：
    # - Electron.app/Contents/MacOS/Electron (主二进制)
    # - Electron.app/Contents/Frameworks/Electron Helper.app/Contents/MacOS/Electron Helper
    # - Electron.app/Contents/Frameworks/Electron Helper (GPU/Renderer/Plugin).app/Contents/MacOS/Electron Helper (GPU/Renderer/Plugin)
    # - Electron.app/Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework
    # - Electron.app/Contents/Frameworks/Electron Framework.framework/Electron Framework (symlink，跳过)
    find ../niu.app/Contents/Resources/ui/main -type f \( -name "Electron" -o -name "Electron Helper*" -o -name "Electron Framework" -o -name "*.node" \) -not -name "*.bak" -not -type l -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true

    echo "[build.sh] signing top-level bundle..."
    codesign --force --sign - ../niu.app 2>/dev/null || true
```

### Step 3: `./launcher/build.sh` 跑一次验证

### Step 4: 验证 bundle 内 python3 自包含

```bash
PYTHONHOME=REDACTED_USER_PATH/tools/ai-bot/niu.app/Contents/Resources/python \
    niu.app/Contents/Resources/python/bin/python3 -c "import encodings; print(encodings.__file__)"
# 必须输出 niu.app/Contents/Resources/python/lib/python3.11/encodings/__init__.py
```

### Step 5: 提交

---

## Task 12: 端到端验证（项目目录）

### Step 1: 杀进程 + 去 quarantine
### Step 2: `open niu.app` 模拟 Finder 双击
### Step 3: 验证三层进程 + 窗口可见 + ~/.niu/config/ 复制
### Step 4: **关键验证**：bundle 内 python3 用 PYTHONHOME 能 import encodings（__file__ 指向 bundle 内）

---

## Task 13: 跨目录验证（/Applications/）

### Step 1: `ditto niu.app /Applications/niu.app` + xattr -cr + codesign + lsregister
### Step 2: `open /Applications/niu.app` 验证
### Step 3: **关键验证**：bundle 内 python3 跨目录后仍能 import（PYTHONHOME 动态传）

---

## Task 14: 开发模式兼容验证

### Step 1: `./niu` 直接跑（dev 模式，detect_resources_root 走 dev 分支）
### Step 2: cargo run（detect_python cwd fallback）
### Step 3: Windows 静态验证

---

## 审查要求

**每个 Task 完成后**：派审查 Agent 做 spec + code quality 审查。

**Task 10 和 Task 12/13 的关键验证**：审查 Agent **必须实际跑** `PYTHONHOME=<bundle>/python python/bin/python3 -c "import encodings; print(encodings.__file__)"` 验证输出指向 bundle 内，**不能采信"已验证"声明**。这是 v6 plan 失败的教训。
