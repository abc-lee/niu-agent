# Splash 启动阶段进度提示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Splash 启动画面从静态"正在启动..."改为显示当前启动阶段（"正在加载向量模型"、"正在加载工具"等），让用户知道后端在做什么。

**Architecture:** Python 端在每个启动阶段开始时调用 `set_preload_stage("中文阶段名")`，设置全局变量。现有 `/api/preload-status` 端点扩展返回 `stage` 字段。Rust 启动器在轮询 preload-status 时解析 `stage`，通过 channel 推给 Splash 窗口显示。不依赖日志配置，不新增通信端口，Python 改动约 15 行 + Rust 改动约 40 行。

**Tech Stack:** Python 3.11 (FastAPI), Rust (iced GUI + reqwest)

---

## 已验证的关键事实

1. **`/api/preload-status` 端点已存在**：`niu_api/compat.py:1713-1716`，当前返回 `{"ready": bool, "uptime": str}`。Rust 端 L1924-1964 已在轮询此端点。
2. **日志关闭时 stderr 无输出**：`niu_api/__main__.py:37-51`，`logging.enabled` 缺省 `False`，`logger.disable("")` 后所有 loguru 输出被丢弃。解析 stderr 不可靠。
3. **Python lifespan 有 ~15 个串行阶段**（`niu_api/__main__.py:54-460`），每个阶段都有 `logger.info`，但我们需要的是不依赖日志的 `set_preload_stage()` 调用。
4. **Rust 后台线程已在轮询 `/api/preload-status`**：L1924-1964，每 500ms 一次，解析 JSON 取 `ready` 字段。
5. **Rust→Splash 通信已有 channel 模式**：`splash_tx`/`splash_rx`（ready 信号）和 `phase_tx`/`phase_rx`（lifecycle 信号）。新增一个 `stage_tx`/`stage_rx` 遵循同样模式。
6. **Splash view 渲染**：`main.rs:805-880`，`label_text` 当前硬编码 "正在启动"/"正在修复"/"正在关闭"。改为从 `stage_rx` 读取最新阶段文本。
7. **`/health` 返回 `embedding_ready` + `scheduler_running`**，但只能分 3 个粗阶段，不够细。
8. **铁律 8**：Rust 改动后必须用 `launcher/build.sh` 编译，禁止直接 `cargo build`。

## Python 启动阶段映射表

| lifespan 顺序 | 阶段文本 | 代码位置 |
|---|---|---|
| 启动前（Python 进程刚 spawn） | 正在启动服务 | Rust 默认值，无需 Python 设置 |
| 1. session store | 正在初始化会话 | `__main__.py:61` |
| 2. embedding preload | 正在加载向量模型 | `__main__.py:70` |
| 3. scheduler + HAWatcher | 正在启动调度器 | `__main__.py:76` |
| 4. MCP tools | 正在加载工具 | `__main__.py:96` |
| 5. runner init | 正在初始化 Agent | `__main__.py:107` |
| 6. WAL + channel + IM Gateway | 正在初始化通信通道 | `__main__.py:117` |
| 6.6. ChatQueue | 正在启动消息队列 | `__main__.py:189` |
| 6.7. LightRAG Phase1 | 正在检查知识图谱 | `__main__.py:194` |
| 7.5. LightRAG eager init | 正在初始化知识图谱 | `__main__.py:269` |
| 8.01-8.026. BrainGraph + regions | 正在初始化脑区 | `__main__.py:300` |
| 8.6. system tasks | 正在加载系统任务 | `__main__.py:355` |
| 8.7. brain region startup gate | 正在同步脑区状态 | `__main__.py:416` |
| 8.8. preload complete | 完成 | `__main__.py:456`（已有 set_preload_complete） |

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `niu_api/compat.py` | 新增 `_preload_stage` 全局变量 + `set_preload_stage()` + 扩展 `/api/preload-status` 返回 stage | 修改 |
| `niu_api/__main__.py` | 在每个阶段开始时调用 `set_preload_stage()` | 修改 |
| `launcher/src/main.rs` | 解析 preload-status 的 stage 字段 + 新增 stage channel + Splash 显示 | 修改 |

---

### Task 1: Python 端 — set_preload_stage() + 扩展 preload-status

**Files:**
- Modify: `niu_api/compat.py` — 新增全局变量和函数，修改端点
- Modify: `niu_api/__main__.py` — 在每个阶段调用 set_preload_stage()

- [ ] **Step 1: 在 compat.py 新增 _preload_stage 变量和 set_preload_stage 函数**

在 `niu_api/compat.py` 中找到 `_preload_complete` 变量定义处（L1133-1143），在它旁边新增：

当前代码（compat.py L1132-1143）：
```python
# Track startup time
_startup_time = datetime.now()

# Preload status
_preload_complete = False


def set_preload_complete():
    """Mark preload as complete"""
    global _preload_complete
    _preload_complete = True
    logger.info("Preload marked as complete")
```

改为：
```python
# Track startup time
_startup_time = datetime.now()

# Preload status
_preload_complete = False
_preload_stage = "正在启动服务"


def set_preload_complete():
    """Mark preload as complete"""
    global _preload_complete, _preload_stage
    _preload_complete = True
    _preload_stage = "启动完成"
    logger.info("Preload marked as complete")


def set_preload_stage(stage: str):
    """Set current preload stage text — called from lifespan at each phase.

    Displayed on the Rust splash window via /api/preload-status.
    Does NOT depend on logging config — always set regardless of log level.
    """
    global _preload_stage
    _preload_stage = stage
    logger.info(f"[STAGE] {stage}")
```

- [ ] **Step 2: 扩展 /api/preload-status 端点返回 stage 字段**

在 `niu_api/compat.py` 中找到 `/api/preload-status` 路由（L1713-1716）：

当前代码：
```python
@router.get("/api/preload-status")
async def get_preload_status():
    """Get preload status - used by Go launcher to wait before showing window"""
    return {"ready": _preload_complete, "uptime": str(datetime.now() - _startup_time).split(".")[0]}
```

改为：
```python
@router.get("/api/preload-status")
async def get_preload_status():
    """Get preload status - used by launcher to wait before showing window"""
    return {
        "ready": _preload_complete,
        "uptime": str(datetime.now() - _startup_time).split(".")[0],
        "stage": _preload_stage,
    }
```

- [ ] **Step 3: 在 __main__.py lifespan 每个阶段调用 set_preload_stage**

在 `niu_api/__main__.py` 的 `lifespan()` 函数中，每个阶段开始处加一行 `set_preload_stage("...")`。

先在文件顶部 import 区（约 L28 `from niu_api.compat import router as compat_router` 之后）加 import。找到现有 import 行：

```python
from niu_api.compat import router as compat_router
```

在它之后加一行：
```python
from niu_api.compat import set_preload_stage
```

然后在 lifespan 函数中，按以下对照表在每个阶段的 `logger.info(...)` 行**之前**插入 `set_preload_stage(...)` 调用。

具体插入点（`__main__.py` 行号基于当前代码）：

在 L61 `# 1. Initialize session store` 之后、L62 `from agent.session import get_session_store` 之前插入：
```python
    set_preload_stage("正在初始化会话")
```

在 L70 `# 2. Preload embedding model` 之后、L71 `from niu_api.internal.embedding import preload as preload_embedding` 之前插入：
```python
    set_preload_stage("正在加载向量模型")
```

在 L76 `# 3. Start internal scheduler` 之后、L77 `from niu_api.internal.scheduler import start_scheduler` 之前插入：
```python
    set_preload_stage("正在启动调度器")
```

在 L96 `# 4. Load MCP tools using ToolRegistry` 之后、L97 `logger.info("Loading MCP tools...")` 之前插入：
```python
    set_preload_stage("正在加载工具")
```

在 L107 `# 5. Initialize runner with ToolRegistry` 之后、L108 `logger.info("Initializing NiuRunner...")` 之前插入：
```python
    set_preload_stage("正在初始化 Agent")
```

在 L117 `# 6.0. Enable SQLite WAL mode` 之后插入：
```python
    set_preload_stage("正在初始化通信通道")
```

在 L189 `# 6.6. Start ChatQueue` 之后、L190 `from niu_api.chat_queue import start_chat_queue` 之前插入：
```python
    set_preload_stage("正在启动消息队列")
```

在 L194 `# 6.7. Phase 1 先跑一致性检测` 注释块之前（L193 空行处）插入：
```python
    set_preload_stage("正在检查知识图谱")
```

在 L269 `# 7.5. Eagerly initialize LightRAG` 之后、L270 注释行之前插入：
```python
        set_preload_stage("正在初始化知识图谱")
```

在 L300 `# 8.01. Initialize Niu self entity` 之后插入：
```python
        set_preload_stage("正在初始化脑区")
```

在 L355 `# 8.6. Ensure system recurring tasks` 之后插入：
```python
        set_preload_stage("正在加载系统任务")
```

在 L416 `# 8.7. Brain region startup gate` 之后、L417 注释之前插入：
```python
    set_preload_stage("正在同步脑区状态")
```

注意：L269/L300/L355 在 `if not _lightrag_corrupt_skip_init:` 块内（4 空格缩进），L416 在块外（4 空格缩进，不在 if 内）。

- [ ] **Step 4: ruff 检查**

Run: `cd /Users/lilei/tools/ai-bot && ruff check niu_api/compat.py niu_api/__main__.py`
Expected: OK

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/compat.py niu_api/__main__.py
git commit -m "feat(api): preload-status 端点返回 stage 字段，lifespan 各阶段设置阶段文本"
```

---

### Task 2: Rust 端 — 解析 stage + Splash 显示

**Files:**
- Modify: `launcher/src/main.rs`

- [ ] **Step 1: 新增 stage channel 创建**

在 `launcher/src/main.rs` 中，找到 channel 创建处（L1732-1737）：

当前代码：
```rust
    let (splash_tx, splash_rx) = std::sync::mpsc::channel::<()>();
    // Lifecycle channel: background thread -> splash window. Used after the
    // settings test passes to (1) flip the splash into the "closing" state
    // showing the shutdown message, and (2) trigger iced::exit() once all
    // child processes have been reaped. See SplashPhase enum for details.
    let (phase_tx, phase_rx) = std::sync::mpsc::channel::<SplashPhase>();
```

在 `let (phase_tx, phase_rx) = ...` 之后加：
```rust
    let (stage_tx, stage_rx) = std::sync::mpsc::channel::<String>();
```

- [ ] **Step 2: 在 Splash struct 新增 stage 字段**

在 `launcher/src/main.rs` 中，找到 `Splash` struct（L88-152）。

在 `missing_deps: Vec<String>,` 字段之后（L151，struct 闭合 `}` 之前）加：
```rust
    /// Missing optional dependencies detected at startup (license-excluded
    /// packages like cv2/insightface/pillow_heif/igraph/leidenalg + buffalo_l
    /// model). Empty = all present. Shown on splash as a hint to read README.
    missing_deps: Vec<String>,
    /// Current startup stage text, updated from preload-status polling.
    /// Displayed instead of the static "正在启动" label.
    stage: String,
}
```

- [ ] **Step 3: 在 Splash::new 初始化 stage**

在 `launcher/src/main.rs` 中，找到 `Splash::new`（L376-401）。

在 `missing_deps,` 之后（L399，`Self {` 块闭合 `}` 之前）加：
```rust
            missing_deps,
            stage: "正在启动服务".to_string(),
        }
```

- [ ] **Step 4: 在后台线程轮询 preload-status 时解析 stage 并发送**

在 `launcher/src/main.rs` 中，找到 preload-status 轮询循环（L1924-1964）。

当前代码：
```rust
        for i in 0..360 {
            thread::sleep(Duration::from_millis(500));
            let url = format!("http://127.0.0.1:{}/api/preload-status", port);
            match check_client.get(&url).send() {
                Ok(resp) => {
                    let body = match resp.text() {
                        Ok(b) => b,
                        Err(_) => continue,
                    };

                    // Parse JSON
                    #[derive(Debug, Deserialize)]
                    struct PreloadStatus {
                        ready: bool,
                        uptime: String,
                    }
                    let status: PreloadStatus = match serde_json::from_str(&body) {
                        Ok(s) => s,
                        Err(e) => {
                            warn!("Failed to parse preload status: error={}, body={}", e, body);
                            continue;
                        }
                    };

                    // Log first response or when ready
                    if i == 0 || status.ready {
                        info!(
                            "Preload status check: ready={}, uptime={}, attempt={}",
                            status.ready, status.uptime, i + 1
                        );
                    }

                    if status.ready {
                        preload_ready = true;
                        info!("Preload complete, launching window...");
                        break;
                    }
                }
                Err(_) => {}
            }
        }
```

改为：
```rust
        for i in 0..360 {
            thread::sleep(Duration::from_millis(500));
            let url = format!("http://127.0.0.1:{}/api/preload-status", port);
            match check_client.get(&url).send() {
                Ok(resp) => {
                    let body = match resp.text() {
                        Ok(b) => b,
                        Err(_) => continue,
                    };

                    // Parse JSON
                    #[derive(Debug, Deserialize)]
                    struct PreloadStatus {
                        ready: bool,
                        uptime: String,
                        stage: Option<String>,
                    }
                    let status: PreloadStatus = match serde_json::from_str(&body) {
                        Ok(s) => s,
                        Err(e) => {
                            warn!("Failed to parse preload status: error={}, body={}", e, body);
                            continue;
                        }
                    };

                    // Forward stage text to splash window
                    if let Some(ref stage) = status.stage {
                        let _ = stage_tx.send(stage.clone());
                    }

                    // Log first response or when ready
                    if i == 0 || status.ready {
                        info!(
                            "Preload status check: ready={}, uptime={}, stage={:?}, attempt={}",
                            status.ready, status.uptime, status.stage, i + 1
                        );
                    }

                    if status.ready {
                        preload_ready = true;
                        info!("Preload complete, launching window...");
                        break;
                    }
                }
                Err(_) => {}
            }
        }
```

关键变化：
1. `PreloadStatus` struct 新增 `stage: Option<String>`（用 Option 兼容旧版本 Python 端不返回 stage 的情况）
2. 解析后 `stage_tx.send(stage.clone())` 推给 Splash
3. 日志增加 stage 字段

- [ ] **Step 5: 将 stage_rx 传入 Splash::new**

在 `launcher/src/main.rs` 中，找到 `Splash::new(...)` 调用处（约 L2240-2245）：

当前代码：
```rust
    let splash = Splash::new(
        splash_rx,
        port,
        cancelled.clone(),
        integrity_failed.clone(),
        phase_rx,
        missing_deps,
    );
```

改为：
```rust
    let splash = Splash::new(
        splash_rx,
        port,
        cancelled.clone(),
        integrity_failed.clone(),
        phase_rx,
        missing_deps,
        stage_rx,
    );
```

- [ ] **Step 6: 更新 Splash::new 签名接收 stage_rx**

在 `launcher/src/main.rs` 中，找到 `Splash::new` 函数签名（L376-401）。

当前代码（Step 2/3 已执行后的状态，已含 stage 字段）：
```rust
    fn new(
        ready_rx: Receiver<()>,
        api_port: u16,
        cancelled: Arc<AtomicBool>,
        integrity_failed: Arc<AtomicBool>,
        phase_rx: Receiver<SplashPhase>,
        missing_deps: Vec<String>,
    ) -> Self {
        Self {
            ready_rx: Mutex::new(ready_rx),
            window_id: None,
            dock_hidden: false,
            dot_frame: 0,
            status_checked: false,
            niu_api_ready: false,
            status_check_completed: false,
            ready_signal_seen: false,
            api_port,
            cancelled,
            integrity_failed,
            repairing: false,
            closing: false,
            phase_rx: Mutex::new(phase_rx),
            missing_deps,
            stage: "正在启动服务".to_string(),
        }
    }
```

改为：
```rust
    fn new(
        ready_rx: Receiver<()>,
        api_port: u16,
        cancelled: Arc<AtomicBool>,
        integrity_failed: Arc<AtomicBool>,
        phase_rx: Receiver<SplashPhase>,
        missing_deps: Vec<String>,
        stage_rx: Receiver<String>,
    ) -> Self {
        Self {
            ready_rx: Mutex::new(ready_rx),
            window_id: None,
            dock_hidden: false,
            dot_frame: 0,
            status_checked: false,
            niu_api_ready: false,
            status_check_completed: false,
            ready_signal_seen: false,
            api_port,
            cancelled,
            integrity_failed,
            repairing: false,
            closing: false,
            phase_rx: Mutex::new(phase_rx),
            missing_deps,
            stage_rx: Mutex::new(stage_rx),
            stage: "正在启动服务".to_string(),
        }
    }
```

同时在 Splash struct 中新增 `stage_rx` 字段（`stage: String` 字段已在 Step 2 中添加，不要重复添加）。
找到 struct 定义中 `stage: String,` 字段之前，加：
```rust
    /// Receiver for stage updates from the background preload-status polling thread.
    /// Wrapped in Mutex for Sync compatibility with iced's runtime.
    stage_rx: Mutex<Receiver<String>>,
```

- [ ] **Step 7: 在 Tick 中读取 stage channel 更新 self.stage**

在 `launcher/src/main.rs` 的 `update` 函数 `SplashMessage::Tick` 分支中，找到 phase_rx 轮询循环之后（L506 之后），ready_signal_seen 检查之前（L519 之前）。

当前代码（L506-519）：
```rust
                }

                // Non-blocking check: if the launcher thread sent the ready signal,
```

在 `}` 和注释之间插入：
```rust
                }

                // Non-blocking drain of stage updates from background thread.
                // Take the latest value (drain all pending, keep last).
                loop {
                    match self.stage_rx.lock().unwrap().try_recv() {
                        Ok(stage_text) => {
                            self.stage = stage_text;
                        }
                        Err(std::sync::mpsc::TryRecvError::Empty) => break,
                        Err(std::sync::mpsc::TryRecvError::Disconnected) => break,
                    }
                }

                // Non-blocking check: if the launcher thread sent the ready signal,
```

- [ ] **Step 8: 在 view 中用 self.stage 替代静态"正在启动"**

在 `launcher/src/main.rs` 的 `view` 函数（L805-880）中，找到 label_text 赋值：

当前代码（L814-820）：
```rust
        let label_text = if self.closing {
            "正在关闭所有进程，关闭后请重新启动程序"
        } else if self.repairing {
            "正在修复"
        } else {
            "正在启动"
        };
```

改为：
```rust
        let label_text: &str = if self.closing {
            "正在关闭所有进程，关闭后请重新启动程序"
        } else if self.repairing {
            "正在修复"
        } else {
            &self.stage
        };
```

注意：`&self.stage` 的类型是 `&String`，不是 `&str`。if/else 表达式要求所有分支类型统一。
显式类型标注 `let label_text: &str` 创建 coercion site，让 `&String` 在此处 deref coerce 为 `&str`，
与 `&'static str` 分支统一。iced 的 `text()` 接受 `impl IntoFragment<'a>`，`&str` 实现了该 trait。

- [ ] **Step 9: 编译验证**

Run: `cd /Users/lilei/tools/ai-bot && ./launcher/build.sh`
Expected: 编译成功，输出 `niu` 二进制到项目根目录

**铁律 8：必须用 launcher/build.sh，禁止直接 cargo build。**

如果编译失败，根据错误信息修复。常见问题：
- `&self.stage` 类型不匹配 → 加显式类型标注 `let label_text: &str = ...`
- `stage_rx` 未导入 → 确认 `use std::sync::mpsc::Receiver` 已在文件顶部

- [ ] **Step 10: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add launcher/src/main.rs
git commit -m "feat(launcher): Splash 显示启动阶段进度文本"
```

---

### Task 3: 运行环境验证

**Files:** 无（手动验证）

- [ ] **Step 1: 重启应用**

```bash
cd /Users/lilei/tools/ai-bot && ./niu
```

- [ ] **Step 2: 观察 Splash 画面**

预期：Splash 文本随启动进度变化：
1. "正在启动服务..."（Python 进程启动前）
2. "正在初始化会话..."
3. "正在加载向量模型..."（停留最久，约 9s）
4. "正在启动调度器..."
5. "正在加载工具..."（约 5s）
6. "正在初始化 Agent..."
7. "正在初始化通信通道..."
8. "正在启动消息队列..."
9. "正在检查知识图谱..."
10. "正在初始化知识图谱..."
11. "正在初始化脑区..."
12. "正在加载系统任务..."
13. "正在同步脑区状态..."（停留最久，最坏 90s）
14. Splash 关闭，主窗口出现

- [ ] **Step 3: 确认日志关闭时仍正常**

在 `config/user-config.json` 或 `config/logging.json` 中确认 `logging.enabled` 为 `false`（缺省值）。
重启应用，确认 Splash 仍然显示阶段文本（不依赖日志）。

---

## Self-Review

### 1. Spec coverage
- ✅ 用户要求"最起码进入哪个阶段显示出来" → 13 个阶段文本覆盖 lifespan 全流程
- ✅ 用户要求"不依赖 Python 回传什么东西"的顾虑 → 方案复用现有 /api/preload-status 端点，不新增端口，但确实需要 Python 端设置 stage 文本。用户已确认接受"扩展现有 preload-status 端点"方案
- ✅ 用户担心"日志关闭读不到" → set_preload_stage() 不依赖日志配置，全局变量始终设置
- ✅ 纯 Rust 端不需要改 → Rust 端解析现有端点的扩展字段

### 2. Placeholder scan
- 无 TBD/TODO
- 所有代码块完整
- 所有插入点都有行号和上下文

### 3. Type consistency
- `_preload_stage: str` (Python) → JSON `stage: string` → Rust `Option<String>` (兼容旧版) → `self.stage: String` → `&str` for label_text（显式类型标注 `let label_text: &str` 触发 deref coercion）
- `set_preload_stage(stage: str)` 签名一致
- `stage_tx`/`stage_rx` channel 类型 `std::sync::mpsc::channel::<String>` 一致
- 不新增 SplashMessage 变体，stage 更新走 channel 轮询（Tick 中 try_recv），不走 iced message 机制

### 4. 已知限制
- stage 文本是中文，Splash 窗口 320px 宽。最长的"正在关闭所有进程，关闭后请重新启动程序"已有 L823 的 `label_size=13` 缩小字体处理。阶段文本最长"正在初始化通信通道"（9 个中文字 + 3 省略号），18px 字体下约 320px，应该能放下。如果溢出，view 函数已有 container 居中处理。
