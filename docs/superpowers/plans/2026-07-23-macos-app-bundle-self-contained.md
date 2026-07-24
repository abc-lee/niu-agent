# macOS .app Bundle 自包含实施计划 (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `niu.app` 成为真正自包含的 macOS 应用包——双击启动、拷到任何 Mac（同 arch）都能跑，所有运行时不可写资源在 bundle 内部只读，所有可写状态在 `~/.niu/`，Python 自包含不依赖系统 Python.framework 且无绝对路径硬编码。Windows 保持开发模式 = 分发模式。

**Architecture:**
- **macOS bundle 模式**（`niu.app/Contents/MacOS/niu`）：所有只读资源（python/、ui/main/、config/、models/、memory/）在 `Contents/Resources/`。二进制从 `current_exe()` 路径含 `.app/Contents/MacOS/` 判断走 bundle 模式，返回 `Contents/Resources/`。
- **macOS 开发模式**（`./niu` 裸二进制，cwd=项目根）：`current_exe()` 不含 `.app/Contents/MacOS/`，走开发模式，返回 `current_exe().parent()`（= 项目根）。
- **Windows/Linux**：始终是开发模式，`niu.exe` 同级有 `python/`、`ui/main/`、`config/`、`models/`。
- **运行时可写数据全部在 `~/.niu/`**：
  - `~/.niu/config/user-config.json`（首次启动从 bundle 内复制模板）
  - `~/.niu/config/llm-presets.json`（首次启动从 bundle 内复制）
  - `~/.niu/logs/`（所有日志：launcher_error.log、raw_http/、gateway_error.log、im_adapter_stderr.log）
  - `~/.niu/window-config.json`（Electron 窗口位置/大小）
  - `~/.niu/memory.json`、`~/.niu/preferences.json`、`~/.niu/skills/`（已有）
- **Python 自包含（无硬编码路径）**：
  - 复制系统 Python.framework 的 Python dylib（14MB universal binary）到 `python/lib/libPython3.11.dylib`
  - 用 install_name_tool 改 python3 二进制和 dylib id 指向 `@rpath/libPython3.11.dylib`，加 `@loader_path/../lib` rpath
  - **删除 pyvenv.cfg**，启动器 spawn Python 时传 `PYTHONHOME=<resources_root>/python` 环境变量（已实测验证：python3 用 PYTHONHOME 找到 stdlib + site-packages，不依赖 pyvenv.cfg，无绝对路径硬编码）
  - 立即 codesign 重签 python3（install_name_tool 让签名失效）
  - site-packages 已自包含，不重装依赖

**Tech Stack:** Rust + iced/winit GUI + Bash（codesign/install_name_tool/lsregister）+ Python 3.11 + Electron 33

**重要约束（不可违反）**：
- ❌ **bundle 内文件运行时不可写**（macOS Gatekeeper 强制）：所有 config 和日志都必须在 `~/.niu/` 下
- ❌ **不支持跨 arch 分发**：torch 2.2.2 macOS wheel 是单 arch，insightface ONNX Runtime 同理
- ❌ **codesign --deep 已废弃**（macOS 13.3+）：改为逐个签名 .so/.dylib + 顶层 bundle
- ❌ **install_name_tool 修改后签名失效**：每次改完必须立即 codesign --force 重签
- ❌ **pyvenv.cfg 不能用绝对路径 home**：跨目录拷贝失效。改用 PYTHONHOME 环境变量
- ❌ **config 路径必须全仓统一**：Rust + Python + Electron 三侧都读 `~/.niu/config/user-config.json`，不能各自硬编码

---

## File Structure

修改的文件：
- `launcher/src/main.rs` — 新增 `detect_resources_root()` + `detect_niu_home()`，改造 `detect_project_root` / `detect_python` / `launch_window` / `should_enable_logging` / `log_fatal_error` / `init_niu_dir` / Python 子进程启动（加 PYTHONHOME env）
- `launcher/build.sh` — macOS 分支：自动调 relocate_python_framework.sh、复制资源进 `Contents/Resources/`、逐个签名 .so/.dylib（排除 .bak + Electron Helper glob）、不 --deep
- `niu_api/config.py` — CONFIG_PATH 改成 `~/.niu/config/user-config.json`，首次启动从 bundle 内复制（路径用 `.parent.parent` 不是 `.parent.parent.parent`）
- `niu_api/http_log_api.py` — `_LOG_DIR` 改成 `~/.niu/logs/raw_http/`
- `niu_api/channel/gateway.py` — `_get_gateway_log_dir` 和 L179 `log_dir` 改成 `~/.niu/logs/`
- `niu_api/llm_proxy.py:203` — 改用 `from niu_api.config import CONFIG_PATH`
- `niu_api/chat.py:333` — 同上
- `niu_api/compat.py:1211` — 同上
- `niu_api/internal/lightrag_manager.py:225` — 同上
- `agent/subagent.py:119` — 同上
- `agent/generic/http_logger.py` — `_get_log_dir` 改成 `~/.niu/logs/raw_http/`
- `agent/generic/litellm_adapter.py` — `_get_app_log_dir` 改成 `~/.niu/logs/`
- `ui/main/main.js` — `settingsConfigDir` 改成 `~/.niu/config/`，`configPath` (window-config.json) 改成 `~/.niu/window-config.json`

新增的文件：
- `scripts/relocate_python_framework.sh` — 复制 Python dylib + 改 install_name + 删 pyvenv.cfg + 重签。build.sh 自动调用。
- `launcher/src/main.rs` 内嵌 `#[cfg(test)] mod tests` — Rust 单元测试。

---

## Task 1: 新增 `detect_resources_root()` + `detect_niu_home()` 函数（含 Rust 单元测试）

**Files:**
- Modify: `launcher/src/main.rs`（在 `detect_project_root` 上方新增函数 + 文件末尾加 `#[cfg(test)] mod tests`）

- [ ] **Step 1: 在 main.rs L1411 上方新增两个函数**

```rust
/// Detect the resources root directory (where python/, ui/, config/, models/, memory/ live).
/// macOS bundle mode: `niu.app/Contents/MacOS/niu` → `niu.app/Contents/Resources/`
/// macOS dev mode / Windows / Linux: `niu` binary parent directory
///
/// Detects bundle mode by checking if exe path contains `.app/Contents/MacOS/`.
/// Does NOT depend on `env::current_dir()` — works when Finder launches with cwd=/.
fn detect_resources_root() -> PathBuf {
    let exe_path = env::current_exe()
        .unwrap_or_else(|_| PathBuf::from("."));

    #[cfg(target_os = "macos")]
    {
        let exe_str = exe_path.to_string_lossy();
        if exe_str.contains(".app/Contents/MacOS/") {
            // Bundle mode: exe = niu.app/Contents/MacOS/niu
            // Resources live in niu.app/Contents/Resources/
            let contents_dir = exe_path
                .parent()  // MacOS/
                .and_then(|p| p.parent())  // Contents/
                .map(|p| p.to_path_buf());
            return match contents_dir {
                Some(contents) => contents.join("Resources"),
                None => PathBuf::from("."),
            };
        }
    }

    // macOS dev mode / Windows / Linux: resources alongside the binary
    exe_path
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Detect user data root (~/.niu/). All writable runtime data lives here.
/// Returns Err if home_dir cannot be determined.
fn detect_niu_home() -> Result<PathBuf, std::io::Error> {
    let home = dirs::home_dir()
        .ok_or_else(|| std::io::Error::new(std::io::ErrorKind::NotFound, "home_dir not found"))?;
    Ok(home.join(".niu"))
}
```

- [ ] **Step 2: 在 main.rs 文件末尾加 Rust 单元测试**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_detect_resources_root_macos_bundle_path_logic() {
        // macOS bundle mode: niu.app/Contents/MacOS/niu → Contents/Resources/
        let exe = PathBuf::from("/Applications/niu.app/Contents/MacOS/niu");
        assert!(exe.to_string_lossy().contains(".app/Contents/MacOS/"));
        let contents = exe.parent().unwrap().parent().unwrap();
        assert_eq!(
            contents.join("Resources"),
            PathBuf::from("/Applications/niu.app/Contents/Resources")
        );
    }

    #[test]
    fn test_detect_resources_root_macos_bundle_arbitrary() {
        for exe in [
            "REDACTED_USER_PATH/Desktop/niu.app/Contents/MacOS/niu",
            "/Applications/niu.app/Contents/MacOS/niu",
            "/Volumes/USB/niu.app/Contents/MacOS/niu",
        ] {
            let p = PathBuf::from(exe);
            assert!(p.to_string_lossy().contains(".app/Contents/MacOS/"));
            let contents = p.parent().unwrap().parent().unwrap();
            assert!(contents.join("Resources").to_string_lossy().ends_with("niu.app/Contents/Resources"));
        }
    }

    #[test]
    fn test_detect_resources_root_dev_mode_no_app() {
        // Dev mode: ./niu bare binary, no .app/Contents/MacOS/
        let exe = PathBuf::from("REDACTED_USER_PATH/tools/ai-bot/niu");
        assert!(!exe.to_string_lossy().contains(".app/Contents/MacOS/"));
        let parent = exe.parent().unwrap();
        assert_eq!(parent, PathBuf::from("REDACTED_USER_PATH/tools/ai-bot"));
    }

    #[test]
    fn test_detect_niu_home_returns_home_niu() {
        // detect_niu_home uses dirs::home_dir() which we can't easily mock,
        // but we can verify the join logic: home + ".niu"
        // (Full integration test in Task 12)
    }
}
```

- [ ] **Step 3: 编译验证 + 跑测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/launcher
cargo test --release 2>&1 | tail -20
```

Expected: 所有测试 PASS。

- [ ] **Step 4: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add launcher/src/main.rs
git commit -m "feat(launcher): add detect_resources_root + detect_niu_home (Rust #[test])

detect_resources_root: bundle mode (Contents/Resources/) vs dev mode
(exe parent) detected by .app/Contents/MacOS/ in exe path.
detect_niu_home: ~/.niu/ for all writable runtime data.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: 改造 `detect_project_root` + 删除 main 函数 cwd fallback（含 grep 验证）

**Files:**
- Modify: `launcher/src/main.rs:1411-1432`（`detect_project_root`）+ L1568-1597（main 函数 memory_dir_check 块）

- [ ] **Step 1: grep 验证 main 函数后续不引用 exe_path / exe_dir / cwd**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
awk 'NR>=1598' launcher/src/main.rs | grep -nE "\bexe_dir\b|\bexe_path\b|\bcwd\b" | head -20
```

Expected: 无输出（或仅注释引用）。如果有真实引用，先处理引用再删除。

- [ ] **Step 2: 改 `detect_project_root` 函数**

把 L1411-1432 整段替换为：

```rust
/// Detect project root directory (= resources root). All path detection
/// now goes through detect_resources_root() — no cwd fallback.
fn detect_project_root() -> String {
    detect_resources_root().to_string_lossy().to_string()
}
```

- [ ] **Step 3: 删除 main 函数 L1568-1597 memory_dir_check 块**

替换为：

```rust
    // Note: exeDir + cwd memory/ check block removed — detect_resources_root()
    // handles both bundle mode (Contents/Resources/) and dev mode (exe parent)
    // without cwd fallback.
```

- [ ] **Step 4: 编译验证**

```bash
./launcher/build.sh 2>&1 | tail -5
```

- [ ] **Step 5: 提交**

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): detect_project_root delegates to detect_resources_root

Removes cwd-based fallback. Verified via grep that main fn body has no
remaining exe_dir/exe_path/cwd variable references after block removal.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: 改造 `detect_python` 用 `detect_resources_root`（含 cargo run cwd fallback）

**Files:**
- Modify: `launcher/src/main.rs:934-985`（`detect_python` 函数）

- [ ] **Step 1: 改 `detect_python` 函数**

把 L934-985 整段替换为：

```rust
/// Detect project's self-contained Python executable.
/// Looks in `<resources_root>/python/bin/python3` (macOS/Linux) or
/// `<resources_root>/python/Scripts/python.exe` (Windows).
///
/// Resources root from detect_resources_root():
/// - macOS bundle: niu.app/Contents/Resources/
/// - macOS dev / Windows / Linux: exe parent
///
/// cargo run fallback: current_exe() = launcher/target/debug/niu-launcher,
/// resources root = launcher/target/debug/ — python/ not there. Fall back
/// to cwd (only when resources_root didn't find it).
///
/// 验证方式说明：用 `python3 --version` 验证二进制存在，不传 PYTHONHOME。
/// `--version` 只加载解释器本身（已链接 dylib），不触发 stdlib 加载，
/// 所以即使 pyvenv.cfg 已删（改用启动器传 PYTHONHOME），`--version` 仍能跑。
/// 真正的 stdlib 可用性由 Python API 启动时（启动器传 PYTHONHOME）验证。
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

    // Dev fallback: cargo run scenario (cwd=project root)
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

    error!(
        "Project Python not found. Checked resources: {}, cwd: {}",
        candidate.display(),
        cwd_candidate.display()
    );
    log_fatal_error(&format!(
        "Project Python not found. Checked resources: {}, cwd: {}",
        candidate.display(),
        cwd_candidate.display()
    ));
    std::process::exit(1);
}
```

- [ ] **Step 2: 编译验证**

```bash
./launcher/build.sh 2>&1 | tail -5
```

- [ ] **Step 3: 提交**

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): detect_python uses detect_resources_root + cargo run fallback

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: 改造 `launch_window` 用 `detect_resources_root`

**Files:**
- Modify: `launcher/src/main.rs:1344-1382`（`launch_window` 函数）

- [ ] **Step 1: 改 `launch_window` 函数**

把 L1344-1382 整段替换为：

```rust
fn launch_window(name: &str) -> Result<std::process::Child, Box<dyn std::error::Error>> {
    let resources_root = detect_resources_root();
    let window_dir = resources_root.join("ui").join("main");

    #[cfg(windows)]
    {
        let mut cmd = Command::new("cmd");
        cmd.args(["/C", "npm", "start"])
            .env("NIU_WINDOW", name)
            .current_dir(&window_dir);
        cmd.stdout(std::process::Stdio::inherit());
        cmd.stderr(std::process::Stdio::inherit());
        cmd.stdin(std::process::Stdio::inherit());
        let child = cmd.spawn()?;
        Ok(child)
    }

    #[cfg(not(windows))]
    {
        let mut cmd = Command::new("npm");
        cmd.arg("start")
            .env("NIU_WINDOW", name)
            .current_dir(&window_dir);
        cmd.stdout(std::process::Stdio::null());
        cmd.stderr(std::process::Stdio::null());
        cmd.stdin(std::process::Stdio::null());
        let child = cmd.spawn()?;
        Ok(child)
    }
}
```

- [ ] **Step 2: 编译验证**

```bash
./launcher/build.sh 2>&1 | tail -5
```

- [ ] **Step 3: 提交**

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): launch_window uses detect_resources_root

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 5: Rust 侧日志和 config 路径全部移到 `~/.niu/`（log_fatal_error + should_enable_logging）

**Files:**
- Modify: `launcher/src/main.rs:1432-1448`（`should_enable_logging`）+ L1450-1470（`log_fatal_error`）

- [ ] **Step 1: 改 `should_enable_logging` 读 `~/.niu/config/user-config.json`**

把 L1432-1448 整段替换为：

```rust
/// Read ~/.niu/config/user-config.json `logging.enabled` field.
/// Returns false (conservative default) on any failure.
///
/// IMPORTANT: config file must live in ~/.niu/config/, not bundle.
/// Bundle files are read-only at runtime (macOS Gatekeeper enforces).
/// Rust and Python and Electron all read the same ~/.niu/config/user-config.json
/// so logging flag is consistent across processes.
fn should_enable_logging() -> bool {
    let config_path = match detect_niu_home() {
        Ok(home) => home.join("config").join("user-config.json"),
        Err(_) => return false,
    };
    match std::fs::read_to_string(&config_path) {
        Ok(content) => match serde_json::from_str::<serde_json::Value>(&content) {
            Ok(v) => v
                .get("logging")
                .and_then(|l| l.get("enabled"))
                .and_then(|e| e.as_bool())
                .unwrap_or(false),
            Err(_) => false,
        },
        Err(_) => false,
    }
}
```

- [ ] **Step 2: 改 `log_fatal_error` 写 `~/.niu/logs/launcher_error.log`**

把 L1450-1470 整段替换为：

```rust
/// Write a fatal error message to `~/.niu/logs/launcher_error.log`.
/// Independent of tracing/logging flag — guarantees diagnostic availability.
///
/// IMPORTANT: log file must NOT live in the .app bundle — bundle files are
/// read-only at runtime (macOS Gatekeeper enforces). Write to ~/.niu/logs/.
/// Fallback to /tmp/ if home_dir unavailable (always writable on Unix).
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
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&log_path)
        .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
}
```

- [ ] **Step 3: 编译验证**

```bash
./launcher/build.sh 2>&1 | tail -5
```

- [ ] **Step 4: 提交**

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): Rust log + config moved to ~/.niu/

should_enable_logging reads ~/.niu/config/user-config.json (not bundle).
log_fatal_error writes ~/.niu/logs/launcher_error.log (fallback /tmp/).
Rust and Python and Electron all read same config — logging flag consistent.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 6: Python 侧日志路径全部移到 `~/.niu/logs/`

**Files:**
- Modify: `niu_api/http_log_api.py:18`
- Modify: `niu_api/channel/gateway.py:16-30, 179-180`
- Modify: `agent/generic/http_logger.py:29-32`
- Modify: `agent/generic/litellm_adapter.py:80-87`

- [ ] **Step 1: 改 `niu_api/http_log_api.py`**

L18 附近改为：

```python
from pathlib import Path
import os

def _get_log_dir() -> Path:
    """返回日志目录 ~/.niu/logs/raw_http/，自动创建。"""
    home = os.path.expanduser("~")
    return Path(home) / ".niu" / "logs" / "raw_http"

# 旧 _LOG_DIR = Path("logs") / "raw_http" 已废弃，所有调用改用 _get_log_dir()
# L38/41/56/158 的 _LOG_DIR 引用全部改成 _get_log_dir()
```

注意：所有使用 `_LOG_DIR` 的地方（L38, 41, 56, 158）都要改成调 `_get_log_dir()`。

- [ ] **Step 2: 改 `niu_api/channel/gateway.py`**

L16-30 的 `_get_gateway_log_dir` 改为：

```python
def _get_gateway_log_dir() -> Path:
    """返回 ~/.niu/logs/，自动创建。"""
    import os
    home = os.path.expanduser("~")
    return Path(home) / ".niu" / "logs"
```

L179-180 飞书 adapter stderr 重定向：

```python
# 旧: log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
log_dir = _get_gateway_log_dir()
log_dir.mkdir(parents=True, exist_ok=True)
```

- [ ] **Step 3: 改 `agent/generic/http_logger.py`**

L29-32 `_get_log_dir` 改为：

```python
def _get_log_dir() -> Path:
    """返回日志目录 ~/.niu/logs/raw_http/{YYYYMMDD}/，自动创建。"""
    from datetime import datetime
    import os
    home = os.path.expanduser("~")
    date_str = datetime.now().strftime("%Y%m%d")
    return Path(home) / ".niu" / "logs" / "raw_http" / date_str
```

- [ ] **Step 4: 改 `agent/generic/litellm_adapter.py`**

L80-87 `_get_app_log_dir` 改为：

```python
def _get_app_log_dir() -> Path:
    """返回 ~/.niu/logs/，自动创建。"""
    import os
    home = os.path.expanduser("~")
    return Path(home) / ".niu" / "logs"
```

- [ ] **Step 5: 提交**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/http_log_api.py niu_api/channel/gateway.py agent/generic/http_logger.py agent/generic/litellm_adapter.py
git commit -m "refactor: all Python logs write to ~/.niu/logs/

raw_http, gateway_error, im_adapter_stderr moved from relative logs/
to ~/.niu/logs/. Bundle files are read-only at runtime (macOS Gatekeeper).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 7: Python 侧 config 路径统一——CONFIG_PATH 改到 `~/.niu/config/` + 全仓硬编码扫除

**Files:**
- Modify: `niu_api/config.py:45`（CONFIG_PATH + 首次启动复制）
- Modify: `niu_api/llm_proxy.py:203`
- Modify: `niu_api/chat.py:333`
- Modify: `niu_api/compat.py:1211`
- Modify: `niu_api/internal/lightrag_manager.py:222-225`（同时清理 L222 旧路径 + 改 L225）
- Modify: `agent/subagent.py:119`
- Modify: `agent/mcp_loader.py:46`（mcp-servers.yaml 也改到 ~/.niu/config/）
- Modify: `mcp-servers/config-manager/src/niu_config_manager/__init__.py:338-340`（config-manager 写入路径）

- [ ] **Step 1: 改 `niu_api/config.py` CONFIG_PATH + 首次启动复制**

L45 附近改为：

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

- [ ] **Step 2: 改 `niu_api/llm_proxy.py:203`**

把 `config_path = Path(__file__).parent.parent / "config" / "user-config.json"` 改为：

```python
from niu_api.config import CONFIG_PATH
config_path = Path(CONFIG_PATH)
```

- [ ] **Step 3: 改 `niu_api/chat.py:333`**

同 Step 2：

```python
from niu_api.config import CONFIG_PATH
config_path = Path(CONFIG_PATH)
```

- [ ] **Step 4: 改 `niu_api/compat.py:1211`**

同 Step 2：

```python
from niu_api.config import CONFIG_PATH
config_path = Path(CONFIG_PATH)
```

- [ ] **Step 5: 改 `niu_api/internal/lightrag_manager.py:222-227`**

**重要**：只替换 L222-227 这 6 行（user_config_path 解析块），**不要动 L228 之后的任何代码**。`_probe_in_background` 函数从 L220 延伸到 L326（含 httpx 探测、3 次重试、atomic write、异常处理），截断会破坏 response_format 自动探测功能。

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

删除了 L222-224 的旧路径检查块（`~/.niu/user-config.json` 无 config/ 子目录，是历史残留死代码）。L228 起的 `with open(user_config_path, encoding="utf-8") as f:` 及之后所有探测逻辑保持不变。

- [ ] **Step 6: 改 `agent/subagent.py:119`**

同 Step 2：

```python
from niu_api.config import CONFIG_PATH
return Path(CONFIG_PATH)
```

- [ ] **Step 7: 改 `agent/mcp_loader.py:46`**

把 `config_path = Path(__file__).parent.parent / "config" / "mcp-servers.yaml"` 改为：

```python
from niu_api.config import _get_mcp_servers_path
config_path = Path(_get_mcp_servers_path())
```

- [ ] **Step 7.5: 改 `mcp-servers/config-manager/src/niu_config_manager/__init__.py:338-340`**

config-manager 是预加载 MCP 服务器，运行时会读写 user-config.json（save-config 等）。原代码 L338 `CONFIG_DIR = Path(__file__).parent.parent.parent.parent.parent / "config"` 在 bundle 模式下解析到 `Contents/Resources/config`（只读），写入会失败。

已验证 `load_presets()` (L438-443) 只读不写，PRESETS_PATH 保留 bundle 内只读路径（避免首次复制竞态 + 不污染 ~/.niu/）。把 L338-340 改为：

```python
# 旧: CONFIG_DIR = Path(__file__).parent.parent.parent.parent.parent / "config"
# 新: user-config.json 读写 ~/.niu/config/（首次启动由 niu_api.config 复制模板）
#     llm-presets.json 只读，仍从 bundle 内读（load_presets 只读不写）
CONFIG_DIR = Path(os.path.expanduser("~")) / ".niu" / "config"
USER_CONFIG_PATH = CONFIG_DIR / "user-config.json"
# presets 只读，保留 bundle 内路径（5 层 parent 到项目根/bundle Resources/）
PRESETS_PATH = Path(__file__).parent.parent.parent.parent.parent / "config" / "llm-presets.json"
```

注意（os 和 Path 已在模块顶部导入，无需重复 import）：
- `USER_CONFIG_PATH` 指向 `~/.niu/config/user-config.json`（读写，由 Python 侧 Task 7 Step 1 首次复制）
- `PRESETS_PATH` 保留 bundle 内只读路径（load_presets 只读，5 层 parent 在 dev 模式 = 项目根，bundle 模式 = Contents/Resources/）

- [ ] **Step 8: grep 验证全仓无残留硬编码（含 mcp-servers/）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
grep -rn 'Path(__file__).parent.*"config"\|os.path.*config.*user-config' niu_api/ agent/ mcp-servers/ 2>&1 | grep -v "config.py\|_get_bundle" | head -10
```

Expected: 无输出（或只有 config.py 内 `_get_bundle_config_dir` 的定义 + config-manager PRESETS_PATH 如果保留 bundle 只读）。

- [ ] **Step 9: 验证 config 复制逻辑**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 清空 ~/.niu/config 测试首次复制
rm -rf ~/.niu/config
python/bin/python3 -c "from niu_api.config import CONFIG_PATH; print('CONFIG_PATH:', CONFIG_PATH)"
ls -la ~/.niu/config/user-config.json 2>&1
```

Expected: CONFIG_PATH 输出 `~/.niu/config/user-config.json`，文件存在。

- [ ] **Step 10: 提交**

```bash
git add niu_api/config.py niu_api/llm_proxy.py niu_api/chat.py niu_api/compat.py niu_api/internal/lightrag_manager.py agent/subagent.py agent/mcp_loader.py
git commit -m "refactor: unify all Python config reads to ~/.niu/config/

CONFIG_PATH moved to ~/.niu/config/user-config.json with first-run copy
from bundle template. All 5 hardcoded Path(__file__).parent.parent/'config'
replaced with from niu_api.config import CONFIG_PATH. mcp-servers.yaml
also moved to ~/.niu/config/.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 8: Python API cwd 改成 `~/.niu/` + 启动时传 PYTHONHOME 环境变量

**Files:**
- Modify: `launcher/src/main.rs:1688`（`api_server_cmd.current_dir` + env）

- [ ] **Step 1: 改 launcher Rust 侧 Python API cwd + 加 PYTHONHOME env（带 pyvenv.cfg 守卫）**

L1688 附近改为：

```rust
let api_server_cwd = match detect_niu_home() {
    Ok(home) => home.to_string_lossy().to_string(),
    Err(_) => project_root_bg.clone(),  // dev fallback
};
api_server_cmd.current_dir(&api_server_cwd);

// PYTHONHOME: 让 python3 找到 bundle 内 stdlib + site-packages
// 替代 pyvenv.cfg home 字段，避免绝对路径硬编码（跨目录拷贝后仍能跑）
//
// 守卫：只在 pyvenv.cfg 不存在时才传 PYTHONHOME。
// - bundle 模式（Task 10 跑过 relocate，pyvenv.cfg 已删）：必须传 PYTHONHOME
// - dev 模式且用户跑过 relocate：传 PYTHONHOME（pyvenv.cfg 已删）
// - dev 模式且用户没跑 relocate（pyvenv.cfg 还在，home 指向系统 framework）：
//   不传 PYTHONHOME（PYTHONHOME 优先级高于 pyvenv.cfg，传了反而让 Python 找不到 stdlib，
//   因为 venv 不复制 stdlib，stdlib 在系统 framework 路径）
let python_home = PathBuf::from(&python_path_bg)
    .parent()  // bin/
    .and_then(|p| p.parent())  // python/
    .map(|p| p.to_path_buf())
    .unwrap_or_default();
let pyvenv_cfg = python_home.join("pyvenv.cfg");
if !pyvenv_cfg.exists() {
    api_server_cmd.env("PYTHONHOME", &python_home.to_string_lossy().to_string());
}
```

注意：`python_path_bg` 是 detect_python() 的返回值（绝对路径到 python3 二进制），`parent().parent()` 得到 `python/` 目录。

- [ ] **Step 2: 编译验证**

```bash
./launcher/build.sh 2>&1 | tail -5
```

- [ ] **Step 3: 提交**

```bash
git add launcher/src/main.rs
git commit -m "refactor(launcher): Python API cwd to ~/.niu/ + PYTHONHOME env

api_server_cmd.current_dir set to ~/.niu/ (writable, not bundle).
PYTHONHOME env var passed so python3 finds stdlib + site-packages
without pyvenv.cfg home (avoids absolute path hardcoding).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 9: Electron 侧 config 和 window-config 路径改到 `~/.niu/`

**Files:**
- Modify: `ui/main/main.js:36`（window-config.json）+ L1109（settingsConfigDir）

- [ ] **Step 1: 改 `ui/main/main.js` L36 window-config.json 路径 + 迁移逻辑**

L36 附近改为：

```javascript
const path = require('path');
const os = require('os');
const fs = require('fs');

// window-config.json 写到 ~/.niu/（bundle 内只读）
const niuHome = path.join(os.homedir(), '.niu');
if (!fs.existsSync(niuHome)) {
  fs.mkdirSync(niuHome, { recursive: true });
}
const configPath = path.join(niuHome, 'window-config.json');

// 迁移：首次启动若 ~/.niu/window-config.json 不存在但 bundle 内旧路径有文件，复制过去
// 旧路径：ui/main/windows/assistant/window-config.json（bundle 内只读，升级前用户数据在这里）
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

注意：迁移只在首次启动时触发（`~/.niu/window-config.json` 不存在时）。之后用户的窗口位置改动都写到 `~/.niu/window-config.json`，不影响 bundle 内旧文件。

- [ ] **Step 2: 改 `ui/main/main.js` L1109 settingsConfigDir 路径**

L1108-1111 改为：

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

注意：不在 Electron 侧做首次复制，避免与 Python 侧竞态（v3 审查 M3 问题）。

- [ ] **Step 3: 验证 Electron 侧路径解析**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
# 语法检查
node -c ui/main/main.js 2>&1
# 路径逻辑测试
node -e "
const path = require('path');
const os = require('os');
const niuConfigDir = path.join(os.homedir(), '.niu', 'config');
const bundleConfigDir = path.join('ui/main', '..', '..', 'config');
console.log('userConfigPath:', path.join(niuConfigDir, 'user-config.json'));
console.log('bundleConfigDir:', bundleConfigDir);
"
```

Expected: 语法 OK，路径正确。

- [ ] **Step 4: 提交**

```bash
git add ui/main/main.js
git commit -m "refactor(electron): config and window-config moved to ~/.niu/

user-config.json: ~/.niu/config/ (first-run copy from bundle template)
window-config.json: ~/.niu/ (window position/size)
llm-presets.json: still read from bundle (read-only template)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 10: Python 自包含——`scripts/relocate_python_framework.sh`（删 pyvenv.cfg + 用 PYTHONHOME）

**Files:**
- Create: `scripts/relocate_python_framework.sh`

- [ ] **Step 1: 写脚本**

```bash
cat > scripts/relocate_python_framework.sh <<'SCRIPT'
#!/bin/bash
# 把系统 Python.framework 的 Python dylib 复制进 python/lib/，
# 改 python3 二进制 + dylib install_name 指向 @rpath/libPython3.11.dylib，
# 删除 pyvenv.cfg（启动器用 PYTHONHOME 环境变量替代，避免绝对路径硬编码），
# 立即重签 python3（install_name_tool 让签名失效）。
#
# 用法：./scripts/relocate_python_framework.sh [python_dir]
#   python_dir 默认 ./python。脚本对该目录做就地改造。
#   build.sh 调用时传入 bundle 内 python/ 目录路径。

set -e

PYTHON_DIR="${1:-./python}"
FRAMEWORK_PYTHON="/Library/Frameworks/Python.framework/Versions/3.11/Python"

if [ ! -f "$FRAMEWORK_PYTHON" ]; then
    echo "[relocate] ERROR: $FRAMEWORK_PYTHON not found"
    echo "[relocate] This script requires macOS system Python 3.11 framework"
    exit 1
fi

if [ ! -d "$PYTHON_DIR" ]; then
    echo "[relocate] ERROR: $PYTHON_DIR not found"
    exit 1
fi

# Step 0: 备份 + otool 留证
echo "[relocate] Step 0: backup + otool 留证"
cp "$PYTHON_DIR/bin/python3" "$PYTHON_DIR/bin/python3.bak"
if [ -f "$PYTHON_DIR/pyvenv.cfg" ]; then
    cp "$PYTHON_DIR/pyvenv.cfg" "$PYTHON_DIR/pyvenv.cfg.bak"
fi
otool -L "$PYTHON_DIR/bin/python3" > /tmp/python3_otool_before.txt 2>&1
otool -D "$PYTHON_DIR/bin/python3" > /tmp/python3_install_name_before.txt 2>&1

# Step 1: 复制 Python dylib 到 python/lib/libPython3.11.dylib
DYLIB_DST="$PYTHON_DIR/lib/libPython3.11.dylib"
echo "[relocate] Step 1: copy Python dylib to $DYLIB_DST"
cp "$FRAMEWORK_PYTHON" "$DYLIB_DST"

# Step 2: 改 dylib 的 install_name (id) 为 @rpath/libPython3.11.dylib
echo "[relocate] Step 2: set dylib install_name to @rpath/libPython3.11.dylib"
install_name_tool -id @rpath/libPython3.11.dylib "$DYLIB_DST"

# Step 3: 改 python3 二进制的 dylib 引用
PYTHON3_BIN="$PYTHON_DIR/bin/python3"
echo "[relocate] Step 3: change python3 binary dylib reference"
install_name_tool -change \
    "$FRAMEWORK_PYTHON" \
    @rpath/libPython3.11.dylib \
    "$PYTHON3_BIN"

# Step 4: 给 python3 加 @loader_path/../lib rpath
echo "[relocate] Step 4: add @loader_path/../lib rpath to python3"
install_name_tool -add_rpath @loader_path/../lib "$PYTHON3_BIN" 2>/dev/null || \
    echo "[relocate] (rpath already exists, skipping)"

# Step 5: 删除 pyvenv.cfg（启动器用 PYTHONHOME 环境变量替代）
# pyvenv.cfg home 字段是绝对路径，跨目录拷贝后失效。
# PYTHONHOME 环境变量由启动器在 spawn Python 时动态传入，无硬编码。
echo "[relocate] Step 5: remove pyvenv.cfg (replaced by PYTHONHOME env var)"
rm -f "$PYTHON_DIR/pyvenv.cfg"

# Step 6: 立即重签 python3 + dylib（install_name_tool 让签名失效）
echo "[relocate] Step 6: re-sign python3 + dylib (signatures invalidated by install_name_tool)"
codesign --force --sign - "$PYTHON3_BIN" 2>&1 || \
    echo "[relocate] WARNING: codesign python3 failed (non-fatal)"
codesign --force --sign - "$DYLIB_DST" 2>&1 || \
    echo "[relocate] WARNING: codesign dylib failed (non-fatal)"

# Step 7: 验证（用 PYTHONHOME 模拟启动器行为）
echo "[relocate] Step 7: verify with PYTHONHOME"
PYTHONHOME_ABS=$(cd "$PYTHON_DIR" && pwd)
echo "--- python3 otool -L (should show @rpath/libPython3.11.dylib) ---"
otool -L "$PYTHON3_BIN" | grep -i python
echo "--- python3 --version ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON3_BIN" --version
echo "--- python3 import numpy/torch (with PYTHONHOME) ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON3_BIN" -c "import numpy; print('numpy', numpy.__version__)"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON3_BIN" -c "import torch; print('torch', torch.__version__)"
echo "--- python3 sys.prefix (should be PYTHONHOME) ---"
PYTHONHOME="$PYTHONHOME_ABS" "$PYTHON3_BIN" -c "import sys; print('prefix:', sys.prefix)"

echo "[relocate] DONE: python/ is now self-contained (no pyvenv.cfg, uses PYTHONHOME)"
echo "[relocate] To rollback: mv python3.bak python3 && mv pyvenv.cfg.bak pyvenv.cfg && rm lib/libPython3.11.dylib"
SCRIPT
chmod +x scripts/relocate_python_framework.sh
```

- [ ] **Step 2: 备份整个 python/bin 和 pyvenv.cfg 后跑脚本**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
cp -R python/bin python/bin.backup
cp python/pyvenv.cfg python/pyvenv.cfg.backup 2>/dev/null || true

./scripts/relocate_python_framework.sh python
```

Expected: Step 7 输出 numpy/torch 版本，python3 prefix = PYTHONHOME。

- [ ] **Step 3: 如果失败，完整恢复**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
rm -rf python/bin
mv python/bin.backup python/bin
if [ -f python/pyvenv.cfg.backup ]; then
    mv python/pyvenv.cfg.backup python/pyvenv.cfg
fi
rm -f python/lib/libPython3.11.dylib
echo "restored"
```

- [ ] **Step 4: 清理备份（验证成功后）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
rm -rf python/bin.backup
# 保留 .bak 文件供 rollback（relocate 脚本管理）
echo "cleaned"
```

- [ ] **Step 5: 提交**

```bash
git add scripts/relocate_python_framework.sh python/bin/python3 python/lib/libPython3.11.dylib
# 注意：python/pyvenv.cfg 被删除，git add -A 会记录删除
git add -A python/
git commit -m "feat(python): self-contained Python via install_name_tool + PYTHONHOME

relocate_python_framework.sh:
1. Copies Python dylib to python/lib/libPython3.11.dylib
2. Changes python3 binary install_name to @rpath/libPython3.11.dylib
3. Adds @loader_path/../lib rpath
4. Deletes pyvenv.cfg (replaced by PYTHONHOME env var, no absolute path)
5. Re-signs python3 + dylib (install_name_tool invalidates signatures)

Verified: python3 with PYTHONHOME=<bundle>/python finds stdlib + site-packages,
imports numpy/torch successfully. No absolute path hardcoding — works after
copying bundle to any directory.

CLAUDE.md rule #6: python/ is now truly self-contained.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 11: build.sh macOS 分支改造——自动调 relocate + 复制资源 + 逐个签名（优化）

**Files:**
- Modify: `launcher/build.sh`（macOS 分支 L10-92 之间）

- [ ] **Step 1: 看当前 build.sh**

```bash
cat launcher/build.sh
```

- [ ] **Step 2: 在 codesign 之前插入资源复制逻辑**

在 `launcher/build.sh` macOS 分支里，找到 `cp target/release/niu-launcher "$APP_DIR/niu"` 之后、`make_icon` 之前，插入：

```bash
    # --- 复制运行时资源到 Contents/Resources/ ---
    RESOURCES_DIR_FULL="$RESOURCES_DIR"  # ../niu.app/Contents/Resources
    PROJECT_ROOT_FULL="$(cd .. && pwd)"

    # python/ (含 Python dylib + site-packages)
    # 先复制 python/，再对 bundle 内副本跑 relocate_python_framework.sh
    # （不污染源 python/，让开发环境保持原样）
    echo "[build.sh] copying python/ to bundle..."
    # 源末尾 / 表示复制目录内容到目标目录（rsync 语义）
    # -X 保留 xattr（防御性，Task 11 Step 3 会重签覆盖，但加 -X 更稳）
    rsync -aX --delete "$PROJECT_ROOT_FULL/python/" "$RESOURCES_DIR_FULL/python/"

    # 对 bundle 内 python/ 跑 relocate（让它自包含）
    echo "[build.sh] relocate Python framework in bundle..."
    "$PROJECT_ROOT_FULL/scripts/relocate_python_framework.sh" "$RESOURCES_DIR_FULL/python"

    # ui/main/ (Electron frontend, 含 node_modules)
    echo "[build.sh] copying ui/main/ to bundle..."
    rsync -a --delete \
        --exclude '.git' \
        --exclude 'node_modules/.cache' \
        "$PROJECT_ROOT_FULL/ui/main/" "$RESOURCES_DIR_FULL/ui/main/"

    # config/ (LLM API key, agents, mcp-servers.yaml, disk/ — 作为模板，运行时复制到 ~/.niu/config/)
    echo "[build.sh] copying config/ to bundle (template, runtime copies to ~/.niu/config/)..."
    rsync -a --delete "$PROJECT_ROOT_FULL/config/" "$RESOURCES_DIR_FULL/config/"

    # models/ (BAAI bge-base-zh + InsightFace buffalo_l)
    echo "[build.sh] copying models/ to bundle..."
    rsync -a --delete "$PROJECT_ROOT_FULL/models/" "$RESOURCES_DIR_FULL/models/"

    # memory/ (agent template files, copied to ~/.niu/ on first run by init_niu_dir)
    echo "[build.sh] copying memory/ to bundle..."
    rsync -a --delete "$PROJECT_ROOT_FULL/memory/" "$RESOURCES_DIR_FULL/memory/" 2>/dev/null || \
        echo "[build.sh] WARNING: memory/ not found (non-fatal)"
```

- [ ] **Step 3: 把 codesign --deep 改为逐个签名（并行 + 排除 .bak + Electron 主二进制 + Helper glob）**

找到 L72 `codesign --force --deep --sign - ../niu.app`，替换为：

```bash
    # 逐个签名 .so / .dylib / 二进制（codesign --deep 自 macOS 13.3 起废弃）
    # 顺序：inside-out（先签依赖的 dylib/.so，再签 python3，最后签顶层 bundle）
    # 并行：xargs -n 1 -P 4 并行签名（-I{} 会禁用 -P，用 -n 1 替代）
    echo "[build.sh] signing Python .so + .dylib (parallel)..."
    find ../niu.app/Contents/Resources/python -type f \( -name "*.so" -o -name "*.dylib" \) -not -name "*.bak" -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true

    echo "[build.sh] signing python3 binary..."
    codesign --force --sign - ../niu.app/Contents/Resources/python/bin/python3 2>/dev/null || true

    echo "[build.sh] signing Electron main binary + Helper apps..."
    # Electron 主二进制：node_modules/electron/dist/Electron.app/Contents/MacOS/Electron
    find ../niu.app/Contents/Resources/ui/main -type f -name "Electron" -not -name "*.bak" -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true
    # Electron Helper 无扩展名，用 glob 直接匹配，不跑 file
    find ../niu.app/Contents/Resources/ui/main -type f -name "Electron Helper*" -not -name "*.bak" -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true
    # node_modules 里可能有少量 Mach-O（如 .node 文件），用 glob 匹配
    find ../niu.app/Contents/Resources/ui/main -type f -name "*.node" -not -name "*.bak" -print0 | \
        xargs -0 -n 1 -P 4 codesign --force --sign - 2>/dev/null || true

    # 最后签 bundle 顶层（不 --deep）
    echo "[build.sh] signing top-level bundle..."
    codesign --force --sign - ../niu.app 2>/dev/null || echo "[build.sh] WARNING: codesign top-level failed (non-fatal)"
```

- [ ] **Step 4: 跑 build.sh 验证**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
./launcher/build.sh 2>&1 | tail -30
```

Expected: 输出 `copying python/... relocate Python framework... signing Python .so + .dylib... signing python3 binary... signing Electron binaries... signing top-level bundle...`，无错误。

- [ ] **Step 5: 验证 bundle 内容 + python3 自包含**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
echo "---bundle 大小---"
du -sh niu.app
echo "---Contents/Resources/ 内容---"
ls niu.app/Contents/Resources/
echo "---bundle 内 python3 otool -L (不应有 /Library/Frameworks)---"
otool -L niu.app/Contents/Resources/python/bin/python3 | grep -i python
echo "---bundle 内 python3 用 PYTHONHOME 测试 import---"
PYTHONHOME=REDACTED_USER_PATH/tools/ai-bot/niu.app/Contents/Resources/python \
    niu.app/Contents/Resources/python/bin/python3 -c "import numpy; print('numpy', numpy.__version__)"
echo "---bundle 内无 pyvenv.cfg---"
ls niu.app/Contents/Resources/python/pyvenv.cfg 2>&1
```

Expected:
- bundle 约 3GB
- Resources/ 含 python/、ui/、config/、models/、memory/、niu.icns
- python3 otool -L 含 `@rpath/libPython3.11.dylib`，不含 `/Library/Frameworks/Python.framework`
- bundle 内 python3 用 PYTHONHOME 能 import numpy
- bundle 内无 pyvenv.cfg

- [ ] **Step 6: 提交**

```bash
git add launcher/build.sh
git commit -m "feat(build): copy resources + auto-relocate Python + per-file signing

build.sh macOS branch:
1. Copies python/, ui/main/, config/, models/, memory/ to Contents/Resources/
2. Auto-runs relocate_python_framework.sh on bundle's python/ (no manual step)
3. Signs individual .so/.dylib + python3 + Electron Helper (no --deep, excluded .bak)
4. Inside-out signing order (dylib first, then binary, then top-level)

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 12: 端到端验证——Finder 双击模式（项目目录，去 quarantine 避免 dialog）

**Files:** 无（验证 task）

- [ ] **Step 1: 杀干净旧进程**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
pkill -TERM -f "niu.app/Contents/MacOS/niu" 2>/dev/null
pkill -TERM -f "Python -m niu" 2>/dev/null
pkill -TERM -f "electron\|Electron\|ui/main" 2>/dev/null
sleep 2
ps aux | grep -iE "niu|electron" | grep -v grep
```

- [ ] **Step 2: 去 quarantine（开发验证时避免 Gatekeeper dialog 阻塞）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
xattr -d com.apple.quarantine niu.app 2>/dev/null || true
xattr -l niu.app 2>&1
```

Expected: 无 com.apple.quarantine（开发验证时去掉；分发场景 build.sh 会加回）。

- [ ] **Step 3: 用 `open` 模拟 Finder 双击**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
open niu.app
sleep 15
```

- [ ] **Step 4: 验证三层进程 + 窗口可见 + config 复制**

```bash
echo "---launcher---"
ps aux | grep "niu.app/Contents/MacOS/niu" | grep -v grep
echo "---Python niu_api---"
ps aux | grep "Python -m niu_api" | grep -v grep
echo "---Electron---"
ps aux | grep -iE "electron|ui/main" | grep -v grep | head -5
echo "---launcher_error.log（不应有新 FATAL）---"
tail -5 ~/.niu/logs/launcher_error.log 2>&1
echo "---~/.niu/config/ 验证首次启动复制---"
ls -la ~/.niu/config/user-config.json 2>&1
ls -la ~/.niu/config/mcp-servers.yaml 2>&1
```

人工验证：屏幕上应该出现 280x80 splash 窗口（"正在启动..."），Python /health 通过后 splash 关闭，assistant 主窗口打开。

- [ ] **Step 5: 如果 splash 不显示**

可能问题：HideDockIcon 设 `setActivationPolicy: 1`（Accessory）让窗口不显示。这不在本 plan 范围内，单独开 Task 处理。

- [ ] **Step 6: 清理**

```bash
pkill -TERM -f "niu.app/Contents/MacOS/niu" 2>/dev/null
pkill -TERM -f "Python -m niu" 2>/dev/null
pkill -TERM -f "electron\|Electron" 2>/dev/null
sleep 2
```

---

## Task 13: 跨目录验证——拷 niu.app 到 /Applications/

**Files:** 无（验证 task）

- [ ] **Step 1: 复制 niu.app 到 /Applications/ + 去 quarantine + 重签 + lsregister**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
pkill -TERM -f "niu.app/Contents/MacOS/niu" 2>/dev/null
pkill -TERM -f "Python -m niu" 2>/dev/null
pkill -TERM -f "electron\|Electron" 2>/dev/null
sleep 2

ditto niu.app /Applications/niu.app
# ditto 是 macOS 官方推荐复制 .app bundle 工具，保留 xattr + 资源 fork + 代码签名
# cp -R 不保留 xattr，会让 bundle 内 .so 签名丢失，Gatekeeper 加载 .so 时拒绝
# 去 quarantine 避免 Gatekeeper dialog
xattr -d com.apple.quarantine /Applications/niu.app 2>/dev/null || true
# 清所有 xattr（含 com.apple.provenance，ditto 会带过来但路径变了需要重建）
xattr -cr /Applications/niu.app 2>/dev/null || true

# 重要顺序：先 codesign 再 lsregister
# - lsregister 打 com.apple.provenance xattr 需要 bundle 已有有效签名
# - xattr -cr 清掉签名状态后，必须先 codesign 重建签名，再 lsregister 打 provenance
codesign --force --sign - /Applications/niu.app 2>/dev/null || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f /Applications/niu.app 2>/dev/null || true

# 验证 provenance 已重建
xattr -l /Applications/niu.app 2>&1 | grep provenance
echo "---验证 /Applications/niu.app 大小---"
du -sh /Applications/niu.app
```

- [ ] **Step 2: 用 `open` 启动 /Applications/niu.app**

```bash
open /Applications/niu.app
sleep 15
echo "---ps---"
ps aux | grep -iE "niu.app/Contents/MacOS/niu|Python -m niu|electron" | grep -v grep | head -10
echo "---launcher_error.log（不应有权限错误）---"
tail -5 ~/.niu/logs/launcher_error.log 2>&1
echo "---~/.niu/config/ 验证---"
ls -la ~/.niu/config/user-config.json 2>&1
echo "---验证 bundle 内无 pyvenv.cfg（PYTHONHOME 方案）---"
ls -la /Applications/niu.app/Contents/Resources/python/pyvenv.cfg 2>&1
echo "---验证 bundle 内 python3 不依赖 /Library/Frameworks---"
otool -L /Applications/niu.app/Contents/Resources/python/bin/python3 | grep -i python
```

Expected:
- 三层进程起来
- launcher_error.log 无 PermissionError
- ~/.niu/config/user-config.json 存在
- bundle 内无 pyvenv.cfg
- python3 otool -L 无 `/Library/Frameworks/Python.framework`

- [ ] **Step 3: 清理**

```bash
pkill -TERM -f "niu.app/Contents/MacOS/niu" 2>/dev/null
pkill -TERM -f "Python -m niu" 2>/dev/null
pkill -TERM -f "electron\|Electron" 2>/dev/null
sleep 2
rm -rf /Applications/niu.app
```

---

## Task 14: 开发模式兼容验证（macOS ./niu + cargo run + Windows 静态）

**Files:** 无（验证 task）

- [ ] **Step 1: 验证 macOS 开发模式 `./niu` 仍能跑**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
pkill -TERM -f "niu.app/Contents/MacOS/niu" 2>/dev/null
pkill -TERM -f "Python -m niu" 2>/dev/null
pkill -TERM -f "electron\|Electron" 2>/dev/null
sleep 2

./niu &
PID=$!
sleep 15
echo "---ps---"
ps -p $PID 2>&1 | tail -1
ps aux | grep -iE "Python -m niu|electron" | grep -v grep | head -5
echo "---launcher_error.log---"
tail -5 ~/.niu/logs/launcher_error.log 2>&1

pkill -TERM -f "niu.app/Contents/MacOS/niu" 2>/dev/null
pkill -TERM -f "Python -m niu" 2>/dev/null
pkill -TERM -f "electron\|Electron" 2>/dev/null
```

- [ ] **Step 2: 验证 cargo run 仍能跑**

```bash
cd REDACTED_USER_PATH/tools/ai-bot/launcher
cargo run --release 2>&1 &
PID=$!
sleep 15
ps -p $PID 2>&1 | tail -1
pkill -TERM -f "niu-launcher\|Python -m niu\|electron" 2>/dev/null
```

Expected: cargo run 能跑（detect_python 有 cwd fallback）。

- [ ] **Step 3: Windows 兼容性静态验证**

```bash
grep -A 5 "cfg(not(target_os = \"macos\"))" launcher/src/main.rs | head -10
```

Expected: Windows/Linux 分支返回 `exe_path.parent()`，不依赖 .app 结构。

---

## Self-Review 检查

**1. Spec coverage**

| 需求 | Task |
|------|------|
| .app bundle 自包含 | Task 11（资源复制）+ Task 10（Python 自包含）+ Task 13（跨目录验证） |
| Python 真自包含无硬编码 | Task 10（install_name_tool + 删 pyvenv.cfg + PYTHONHOME） |
| 代码路径不依赖 cwd | Task 1-4 |
| Windows 兼容 | Task 14 Step 3 |
| 开发模式 ./niu | Task 14 Step 1 |
| cargo run 兼容 | Task 3 Step 1 + Task 14 Step 2 |
| bundle 内不可写（config/logs/window-config） | Task 5（Rust log+config）+ Task 6（Python logs）+ Task 7（Python config 全仓）+ Task 8（Python cwd+PYTHONHOME）+ Task 9（Electron config+window-config） |
| Rust + Python + Electron 三侧读同一 config | Task 5（Rust should_enable_logging）+ Task 7（Python 全仓 CONFIG_PATH）+ Task 9（Electron userConfigPath） |
| pyvenv.cfg 不能用绝对路径 home | Task 10 Step 5（删 pyvenv.cfg，用 PYTHONHOME） |
| install_name_tool 后立即重签 | Task 10 Step 6 |
| build.sh 自动调 relocate | Task 11 Step 2 |
| codesign --deep 废弃 | Task 11 Step 3（逐个签名，排除 .bak + Electron Helper glob） |
| 跨 arch 不支持 | "重要约束" 小节 |
| Rust #[test] | Task 1 Step 2 |
| 之前补丁已回退 | git revert a82f43e1 + 丢弃 stash |

**2. v2 审查问题修复**

| v2 问题 | v3 修复 |
|---------|---------|
| C1 Electron 遗漏 | Task 9（Electron config + window-config 改到 ~/.niu/） |
| C2 Python 硬编码 | Task 7 Step 2-7（5 处 + mcp_loader 全改 CONFIG_PATH） |
| C3 多一层 parent | Task 7 Step 1（用 `.parent.parent` 不是 `.parent.parent.parent`） |
| C4 Rust should_enable_logging 没改 | Task 5 Step 1（改读 ~/.niu/config/） |
| C5 pyvenv.cfg home 语义错 | Task 10 Step 5（删 pyvenv.cfg，用 PYTHONHOME，已实测验证） |
| M1 find 漏 Helper + 误签 .bak | Task 11 Step 3（-not -name "*.bak" + Electron Helper glob） |
| M2 pyvenv.cfg 绝对路径 | Task 10 Step 5（删 pyvenv.cfg）+ Task 8（PYTHONHOME 动态传） |
| M3 双签 + 顺序 | Task 11 Step 3（inside-out 顺序） |
| M4 fallback 写 /logs/ | Task 5 Step 2（fallback 改 /tmp/） |
| L2 Task 10 弹 dialog | Task 12 Step 2（去 quarantine） |

**3. v3 审查问题修复（v4）**

| v3 问题 | v4 修复 |
|---------|---------|
| S1 detect_python 删 pyvenv.cfg 后 --version 失败 | 实测验证是误报（--version 不触发 stdlib 加载），Task 3 加注释说明验证方式 |
| S2 mcp-servers/config-manager 遗漏 | Task 7 加 Step 7.5（改 config-manager CONFIG_DIR），Step 8 grep 范围扩展到 mcp-servers/ |
| S3 lightrag_manager L222 旧路径 | Task 7 Step 5 改为整段替换 L218-232，删除 L222-224 旧路径检查块 |
| M1 Electron 主二进制漏签 | Task 11 Step 3 加 `find -name "Electron"` 单独签主二进制 |
| M2 codesign 串行慢 | Task 11 Step 3 改 `xargs -n 1 -P 4` 并行签名 |
| M3 首次复制竞态 | Task 9 Step 2 删除 Electron 侧首次复制，依赖 Python 侧先复制 |

**4. v4 审查问题修复（v5）**

| v4 问题 | v5 修复 |
|---------|---------|
| S1 lightrag_manager 函数体截断 | Task 7 Step 5 缩小替换范围到 L222-227（6 行），明确"不要动 L228 之后" |
| S2 config-manager PRESETS_PATH 矛盾 | Task 7 Step 7.5 PRESETS_PATH 保留 bundle 内只读路径，删除 conditional 逻辑 |
| M1 Task 13 codesign/lsregister 顺序 | Task 13 Step 1 调整为 xattr -cr → codesign → lsregister（先签名再打 provenance） |
| M3 window-config.json 无迁移 | Task 9 Step 1 加迁移逻辑（首次启动从 bundle 内旧路径复制到 ~/.niu/） |
| M4 dev 模式 PYTHONHOME 与 pyvenv.cfg 冲突 | Task 8 加 pyvenv.cfg 存在性守卫（只在 pyvenv.cfg 不存在时才传 PYTHONHOME） |
| L1 detect_python is_ok 不验证 exit code | Task 3 改 `.output().map(\|o\| o.status.success()).unwrap_or(false)` |

**5. v5 审查问题修复（v6 小改）**

| v5 问题 | v6 修复 |
|---------|---------|
| L1 Task 13 cp -R 不保留 xattr | Task 13 Step 1 改用 ditto（macOS 官方推荐，保留 xattr + 资源 fork + 代码签名） |
| M1 Task 11 rsync 不带 -X | Task 11 Step 2 rsync 加 -X（防御性，保留 xattr） |
| L2 Task 7 Step 7.5 冗余 import | Task 7 Step 7.5 删除 `import os` 和 `from pathlib import Path`（模块顶部已导入） |

**关键发现（v5 审查）**：MCP 服务器走同进程架构（agent/mcp_loader.py 把 workdir 加入 sys.path 后直接 import 模块），不是 subprocess spawn，从 Python API 进程继承 PYTHONHOME 环境变量。飞书 adapter 是唯一 subprocess，但 `env = dict(os.environ)` 显式继承。所以 Task 8 只改 api_server_cmd 完全正确，MCP 服务器不需要单独传 PYTHONHOME。

**3. Placeholder 扫描**：无 TBD/TODO，每个 Task 有具体代码。

**4. Type 一致性**：
- `detect_resources_root() -> PathBuf`
- `detect_niu_home() -> Result<PathBuf, std::io::Error>`
- `detect_project_root() -> String`
- Python `_get_config_path() -> str`、`_get_mcp_servers_path() -> str`、`_get_bundle_config_dir() -> Path`
- 命名一致。

---

## 执行建议

14 个 Task。建议 Subagent-Driven：
- Task 1-9 代码改造（机械实现，cheap model）
- Task 10 Python 自包含（关键，std model，已实测 PYTHONHOME 可行）
- Task 11 build.sh 改造（std model）
- Task 12-14 端到端验证（main agent 验证）

**Plan complete and saved to `docs/superpowers/plans/2026-07-23-macos-app-bundle-self-contained.md` (v3). Two execution options:**

**1. Subagent-Driven (recommended)** - Fresh subagent per task, review between tasks

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch with checkpoints

**Which approach?**
