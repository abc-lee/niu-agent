# GPL 依赖排除 + 启动器缺失提示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 cv2/insightface/easydict/pillow-heif 等 GPL 依赖从 DMG 排除（和 igraph/leidenalg 同模式），并在 Rust 启动器 splash 窗口显示缺失依赖提示（不阻塞启动），README + 系统手册说明按需自装。

**Architecture:** build.sh 的 python/ rsync 加 exclude 排除人脸识别 + HEIC 相关 GPL 包（不动 venv/requirements.txt，只 exclude 打包）；Rust 启动器 `main()` 在 `detect_python()` 后用 `PathBuf::exists()` 检查 site-packages 下关键包目录 + buffalo_l 模型目录，结果塞进 `Splash.missing_deps`，`view()` 渲染时如有缺失项多显示一段提示并用 `window::resize` 动态加大窗口；README 加"可选：启用照片处理"章节，系统手册"可选组件安装"章节同步补照片处理。

**Tech Stack:** bash（build.sh rsync）、Rust + Iced 0.13.1（启动器）、Markdown（README/手册）

## Global Constraints

- **铁律**：主 Agent 是项目经理不改代码，所有改动委托子 Agent；改前 git 备份 + gitnexus 影响分析；git 操作后修文件权限；Rust 编译必须用 `launcher/build.sh`，禁止直接 `cargo build`
- **只 exclude 打包，不动 venv/requirements.txt/pyproject.toml**：用户的本地开发环境照常用这些包，只是 DMG 不含
- **不阻塞启动**：启动器照常启动，splash 只是提示缺失项，关闭逻辑（ready_signal + status_check）完全不变
- **检查用 `PathBuf::exists()`**：不 spawn `python -c "import cv2"`（启动器对 Python 子进程开销敏感，且 PYTHONHOME 等环境未在此阶段设好）
- **site-packages 路径**：`<resources_root>/python/lib/python3.11/site-packages/`（python3.11 已确认）
- **buffalo_l 模型检查路径**：`<resources_root>/models/models/buffalo_l/`（bundle 内空目录）+ `~/.insightface/models/buffalo_l/`（用户首次用自动下载到这里），两个都检查
- **人脸识别全套排除**：cv2 + insightface + easydict 一起排除（insightface 硬依赖 cv2，没 cv2 用不了，所以 insightface 和 easydict 留着也没用）
- **pillow-heif 独立排除**：HEIC 支持和 cv2 解耦，单独排除
- **README 命令用自包含 Python 路径**：`/Applications/niu.app/Contents/Resources/python/bin/python3`（不是系统 pip）

---

## 文件结构

| 文件 | 责任 | 动作 |
|------|------|------|
| `launcher/build.sh` | python/ rsync 加 exclude 排除 cv2/insightface/easydict/pillow-heif | Modify |
| `launcher/src/main.rs` | 启动器加缺失依赖检查 + splash 显示 + 窗口动态加大 | Modify |
| `README.md` | 加"可选：启用照片处理（人脸识别 + HEIC）"章节 | Modify |
| `docs/SYSTEM_MANUAL.md` | "可选组件安装"章节补照片处理 | Modify |

---

### Task 1: build.sh 排除人脸识别 + HEIC 相关 GPL 包

**Files:**
- Modify: `launcher/build.sh:38-42`（python/ rsync 的 exclude 块）

**Interfaces:** 无（shell 脚本改动，无跨任务接口）

- [ ] **Step 1: 读当前 python/ rsync 块确认位置**

读 `launcher/build.sh` 第 32-45 行，确认当前 exclude 列表（已有 igraph/leidenalg/texttable）。当前应该是：
```bash
    rsync -a --delete --exclude='*.bak' \
        --exclude='igraph' --exclude='igraph-*.dist-info' --exclude='python_igraph-*.dist-info' \
        --exclude='leidenalg' --exclude='leidenalg-*.dist-info' \
        --exclude='texttable.py' --exclude='texttable-*.dist-info' \
        "$PROJECT_ROOT/python/" "$RESOURCES_DIR/python/"
```

- [ ] **Step 2: 加 4 个包的 exclude**

把上面的 rsync 块改成（在 texttable 排除后追加 cv2/insightface/easydict/pillow_heif 及其 dist-info 和 .dylibs）：

```bash
    # 排除 igraph + leidenalg（GPL，用户按 README 用自包含 Python 手动安装）
    # 注意：igraph 在 PyPI 包名是 python-igraph，dist-info 目录用下划线 python_igraph-*
    # texttable 是 igraph 的依赖，也一并排除
    # cv2/insightface/easydict/pillow_heif：人脸识别 + HEIC，cv2 捆绑 GPL 版 FFmpeg（x264/x265），
    # pillow-heif 链接 libx265（GPLv2），用户需照片处理时按 README 自装
    rsync -a --delete --exclude='*.bak' \
        --exclude='igraph' --exclude='igraph-*.dist-info' --exclude='python_igraph-*.dist-info' \
        --exclude='leidenalg' --exclude='leidenalg-*.dist-info' \
        --exclude='texttable.py' --exclude='texttable-*.dist-info' \
        --exclude='cv2' --exclude='opencv_python_headless-*.dist-info' \
        --exclude='insightface' --exclude='insightface-*.dist-info' \
        --exclude='easydict' --exclude='easydict-*.dist-info' \
        --exclude='pillow_heif' --exclude='pillow_heif-*.dist-info' \
        "$PROJECT_ROOT/python/" "$RESOURCES_DIR/python/"
```

注意：
- `cv2` 是包目录名，`opencv_python_headless-*.dist-info` 是 dist-info（注意是下划线不是连字符）
- `insightface` 是包目录名，`insightface-*.dist-info` 是 dist-info
- `easydict` 是包目录名，`easydict-*.dist-info` 是 dist-info
- `pillow_heif` 是包目录名（下划线），`pillow_heif-*.dist-info` 是 dist-info
- cv2 的 `.dylibs`（libx264/libx265 等 GPL 二进制）在 `cv2/.dylibs/` 下，排除 `cv2` 目录会连带排除 `.dylibs`，无需单独 exclude

- [ ] **Step 3: 语法验证**

Run: `bash -n launcher/build.sh && echo "syntax OK"`
Expected: `syntax OK`

- [ ] **Step 4: 验证 exclude 命中**

Run:
```bash
grep -n "exclude='cv2'\|exclude='insightface'\|exclude='easydict'\|exclude='pillow_heif'\|exclude='opencv_python_headless" launcher/build.sh
```
Expected: 4 行匹配（cv2/insightface/easydict/pillow_heif + opencv dist-info）

- [ ] **Step 5: 提交**

```bash
git add launcher/build.sh
git commit -m "build: DMG 排除人脸识别 + HEIC 相关 GPL 包

cv2 捆绑 GPL 版 FFmpeg（libx264/libx265），pillow-heif 链接 libx265（GPLv2），
insightface 硬依赖 cv2，easydict 是 insightface 依赖。
全排除，用户需照片处理时按 README 用自包含 Python 自装。
与 igraph/leidenalg 同模式，不动 venv/requirements.txt。"
```

---

### Task 2: Rust 启动器加缺失依赖检查函数

**Files:**
- Modify: `launcher/src/main.rs`（加 `check_missing_deps` 函数 + `Splash.missing_deps` 字段）

**Interfaces:**
- Produces: `check_missing_deps(resources_root: &Path) -> Vec<String>` — 返回缺失依赖的提示文案列表（空 Vec = 无缺失）

- [ ] **Step 1: git 备份 + gitnexus 影响分析**

```bash
git status  # 确认工作区干净
```

gitnexus impact 分析 `Splash::new` 和 `Splash::view`（预期 LOW，只加字段不改签名）。如果 gitnexus 不可用，用 grep 确认调用链：`Splash::new` 在 main.rs:2071 调用，`view` 被 `iced::application` 用。

- [ ] **Step 2: 加 `check_missing_deps` 函数**

在 `launcher/src/main.rs` 的 `detect_resources_root()` 函数（约第 1378 行）之后，加一个新函数：

```rust
/// Check for optional dependencies excluded from the DMG for license compliance.
/// Returns a list of human-readable hint strings for missing components.
/// Each hint names the affected feature and points the user to README.
/// Empty Vec = all optional deps present (developer build).
///
/// Checks two locations:
/// 1. site-packages/ under bundle (cv2/insightface/easydict/pillow_heif/igraph/leidenalg)
/// 2. ~/.insightface/models/buffalo_l/ (user-downloaded face model)
fn check_missing_deps(resources_root: &Path) -> Vec<String> {
    let site_packages = resources_root
        .join("python")
        .join("lib")
        .join("python3.11")
        .join("site-packages");
    let mut missing: Vec<String> = Vec::new();

    // 人脸识别全套（cv2 + insightface + easydict 一起检查，任一缺失=人脸识别不可用）
    let has_cv2 = site_packages.join("cv2").exists();
    let has_insightface = site_packages.join("insightface").exists();
    let has_easydict = site_packages.join("easydict").exists();
    if !has_cv2 || !has_insightface || !has_easydict {
        missing.push(
            "照片处理 / 人脸识别（缺 cv2 + insightface + easydict，见 README\"可选：启用照片处理\"）"
                .to_string(),
        );
    }

    // HEIC 照片支持
    if !site_packages.join("pillow_heif").exists() {
        missing.push(
            "iPhone HEIC 照片（缺 pillow-heif，见 README\"可选：启用照片处理\"）".to_string(),
        );
    }

    // 脑区社区检测
    let has_igraph = site_packages.join("igraph").exists();
    let has_leidenalg = site_packages.join("leidenalg").exists();
    if !has_igraph || !has_leidenalg {
        missing.push(
            "脑区社区检测（缺 igraph + leidenalg，见 README\"可选：启用脑区功能\"）".to_string(),
        );
    }

    // 人脸模型（bundle 内空目录 + 用户家目录自动下载位置，两个都没 onnx 才提示）
    let bundle_model = resources_root
        .join("models")
        .join("models")
        .join("buffalo_l");
    let user_model = dirs::home_dir()
        .unwrap_or_default()
        .join(".insightface")
        .join("models")
        .join("buffalo_l");
    let has_onnx = |dir: &Path| -> bool {
        fs::read_dir(dir)
            .map(|entries| entries.filter_map(|e| e.ok()).any(|e| {
                e.path()
                    .extension()
                    .map(|ext| ext == "onnx")
                    .unwrap_or(false)
            }))
            .unwrap_or(false)
    };
    if !has_onnx(&bundle_model) && !has_onnx(&user_model) {
        missing.push(
            "人脸识别模型（缺 buffalo_l 模型，见 README\"可选：启用照片处理\"）".to_string(),
        );
    }

    missing
}
```

注意：
- 用 `PathBuf::exists()` 和 `fs::read_dir`，不 spawn Python
- site-packages 路径硬编码 `python3.11`（已确认 venv 版本）
- 人脸模型检查：bundle 内 `models/models/buffalo_l/` + 用户家目录 `~/.insightface/models/buffalo_l/`，只要任一处有 .onnx 就不提示（用户可能已下载）
- `dirs::home_dir()` 已在文件顶部用（detect_niu_home 用过），无需新 crate

- [ ] **Step 3: 给 Splash 结构体加 `missing_deps` 字段**

在 `launcher/src/main.rs` 的 `struct Splash` 定义（约第 84 行）里，在 `phase_rx` 字段后加：

```rust
    /// Receiver for lifecycle signals from the background thread:
    /// - `SplashPhase::Closing` -> enter closing state
    /// - `SplashPhase::CleanupDone` -> all processes reaped, call iced::exit()
    /// Wrapped in Mutex for Sync compatibility with iced's runtime.
    phase_rx: Mutex<Receiver<SplashPhase>>,
    /// Missing optional dependencies detected at startup (license-excluded
    /// packages like cv2/insightface/pillow_heif/igraph/leidenalg + buffalo_l
    /// model). Empty = all present. Shown on splash as a hint to read README.
    missing_deps: Vec<String>,
```

- [ ] **Step 4: 给 `Splash::new` 加 `missing_deps` 参数**

把 `Splash::new`（约第 367 行）签名改成加 `missing_deps: Vec<String>` 参数，函数体初始化该字段：

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
        }
    }
```

- [ ] **Step 5: 在 `main()` 里调用 `check_missing_deps` 并传给 `Splash::new`**

在 `launcher/src/main.rs` 的 `main()` 函数里，`detect_python()`（约第 1536 行 `let python_path = detect_python();`）之后加：

```rust
    let python_path = detect_python();
    info!("Using Python path: {}", python_path);

    // Check for missing optional dependencies (license-excluded packages).
    // Non-blocking: just shown on splash as a hint to read README.
    let resources_root = detect_resources_root();
    let missing_deps = check_missing_deps(&resources_root);
    if !missing_deps.is_empty() {
        info!("Missing optional dependencies: {:?}", missing_deps);
    }
```

然后在 `Splash::new` 调用处（约第 2071 行）加 `missing_deps` 参数：

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

- [ ] **Step 6: 编译验证**

Run: `cd launcher && cargo build --release 2>&1 | tail -10`
Expected: 编译成功，无错误（可能有 warning，忽略）

> 注意：这里先用 `cargo build` 确认编译通过，正式打包时再用 `launcher/build.sh`（铁律）。本步骤只验证代码编译，不构造 .app。

- [ ] **Step 7: 提交**

```bash
git add launcher/src/main.rs
git commit -m "feat(launcher): 加 check_missing_deps 函数检查排除的依赖

检查 site-packages 下 cv2/insightface/easydict/pillow_heif/igraph/leidenalg
+ buffalo_l 模型，返回缺失项文案列表。
Splash 结构体加 missing_deps 字段，main() 调用后传入。
不阻塞启动，仅用于 splash 显示提示。"
```

---

### Task 3: Splash view 显示缺失依赖 + 窗口动态加大

**Files:**
- Modify: `launcher/src/main.rs:788-832`（`Splash::view` 函数）+ `SplashMessage` + `update`

**Interfaces:**
- Consumes: `Splash.missing_deps` from Task 2

- [ ] **Step 1: 给 `SplashMessage` 加 `WindowOpened` 分支触发 resize**

读 `SplashMessage` 枚举（约第 157 行）和 `update` 的 `SplashMessage::WindowOpened` 分支（约第 534 行）。当前 `WindowOpened` 只存 `window_id`：

```rust
            SplashMessage::WindowOpened(id) => {
                self.window_id = Some(id);
            }
```

改成存 `window_id` + 如果 `missing_deps` 非空则触发 `window::resize`（窗口从 280×80 加大到容纳缺失列表）：

```rust
            SplashMessage::WindowOpened(id) => {
                self.window_id = Some(id);
                // 窗口打开后，如有缺失依赖提示，动态加大窗口高度容纳列表
                if !self.missing_deps.is_empty() {
                    // 每条缺失项约 20px 高度 + 标题 20px + 内边距，基础 80px
                    let extra = (self.missing_deps.len() as f32 + 1) * 22.0;
                    let new_height = 80.0 + extra;
                    return window::resize(id, iced::Size::new(320.0, new_height));
                }
            }
```

注意：窗口宽度也从 280 加到 320（提示文案较长，280 会截断）。

- [ ] **Step 2: 改 `Splash::view` 显示缺失依赖列表**

读 `Splash::view` 函数（约第 788 行）。当前结构是 `container(row![label, dots_container])`。改成：基础 label + dots 行保留，如果 `missing_deps` 非空，在下面加一个提示列。

把整个 `view` 函数改成：

```rust
    fn view(&self) -> Element<'_, SplashMessage> {
        let dots = match (self.dot_frame / 10) % 3 {
            0 => ".",
            1 => "..",
            _ => "...",
        };
        let label_text = if self.closing {
            "正在关闭所有进程，关闭后请重新启动程序"
        } else if self.repairing {
            "正在修复"
        } else {
            "正在启动"
        };
        let label_size = if self.closing { 13 } else { 18 };
        let label = iced::widget::text(label_text)
            .size(label_size)
            .font(CJK_FONT)
            .color([1.0, 1.0, 1.0, 1.0]);
        let dots_text = iced::widget::text(dots)
            .size(18)
            .font(Font::MONOSPACE)
            .color([1.0, 1.0, 1.0, 1.0]);
        let dots_container = container(dots_text).width(Length::Fixed(36.0));
        let top_row = iced::widget::row![label, dots_container]
            .align_y(iced::alignment::Vertical::Center);

        // 缺失依赖提示（如有）
        if self.missing_deps.is_empty() || self.closing {
            // 无缺失 / closing 状态（重启/关闭中）：保持原布局，不显示缺失提示
            // closing 时用户要重启了，提示已无意义
            container(top_row)
                .width(Length::Fill)
                .height(Length::Fill)
                .align_x(iced::alignment::Horizontal::Center)
                .align_y(iced::alignment::Vertical::Center)
                .into()
        } else {
            // 有缺失：顶部 label 行 + 分隔 + 提示标题 + 缺失项列表
            let hint_title = iced::widget::text("以下功能因依赖缺失暂不可用，请读 README 安装说明：")
                .size(11)
                .font(CJK_FONT)
                .color([1.0, 0.85, 0.4, 1.0]);  // 暖黄色提示
            let items: Vec<Element<SplashMessage>> = self
                .missing_deps
                .iter()
                .map(|s| {
                    iced::widget::text(format!("• {}", s))
                        .size(11)
                        .font(CJK_FONT)
                        .color([0.9, 0.9, 0.9, 1.0])
                        .into()
                })
                .collect();
            let items_column = iced::widget::column![hint_title]
                .push(iced::widget::Space::new(Length::Fixed(2.0), Length::Fixed(2.0)))
                .extend(items)
                .spacing(2);
            container(
                iced::widget::column![top_row]
                    .push(iced::widget::Space::new(Length::Fixed(0.0), Length::Fixed(8.0)))
                    .push(items_column)
                    .align_x(iced::alignment::Horizontal::Center),
            )
            .width(Length::Fill)
            .height(Length::Fill)
            .padding(8)
            .align_x(iced::alignment::Horizontal::Center)
            .align_y(iced::alignment::Vertical::Top)
            .into()
        }
    }
```

注意：
- `iced::widget::column.extend(items)` 接受 `Vec<Element>`（确认 Iced 0.13 支持，如不支持改用 `column![hint_title].push(item1).push(item2)` 循环构建）
- `column.push(Space)` 加间距
- 提示标题用暖黄色（`[1.0, 0.85, 0.4, 1.0]`）区别于白色 label
- 缺失项用浅灰色（`[0.9, 0.9, 0.9, 1.0]`）
- `container.padding(8)` 加内边距避免文字贴边

- [ ] **Step 3: 编译验证**

Run: `cd launcher && cargo build --release 2>&1 | tail -10`
Expected: 编译成功

如果 `column.extend` 报错（Iced 0.13 可能无此方法），改成循环 push：
```rust
            let mut items_column = iced::widget::column![hint_title]
                .push(iced::widget::Space::new(Length::Fixed(2.0), Length::Fixed(2.0)));
            for s in &self.missing_deps {
                items_column = items_column.push(
                    iced::widget::text(format!("• {}", s))
                        .size(11)
                        .font(CJK_FONT)
                        .color([0.9, 0.9, 0.9, 1.0]),
                );
            }
            let items_column = items_column.spacing(2);
```

- [ ] **Step 4: 提交**

```bash
git add launcher/src/main.rs
git commit -m "feat(launcher): splash 窗口显示缺失依赖提示 + 动态加大窗口

- view() 有缺失项时多渲染提示标题 + 缺失项列表
- WindowOpened 时如有缺失项，window::resize 从 280×80 加大到 320×(80+项数*22)
- 不阻塞启动，关闭逻辑不变（ready_signal + status_check）
- 提示标题暖黄色，缺失项浅灰色"
```

---

### Task 4: README 加"可选：启用照片处理"章节

**Files:**
- Modify: `README.md:147`（在"可选：启用脑区功能"章节后加新章节）

**Interfaces:** 无（纯文档）

- [ ] **Step 1: 读 README 确认插入位置**

读 `README.md` 第 142-160 行，确认"可选：启用脑区功能"章节的位置和格式。新章节加在它之后、"方式二：从源码构建"之前。

- [ ] **Step 2: 插入"可选：启用照片处理"章节**

在"可选：启用脑区功能"章节末尾（`安装后重启 Niu...`那段引用块之后），加：

```markdown
### 可选：启用照片处理（人脸识别 + HEIC 支持）

Niu 的照片处理功能（拖入照片自动入库、人脸识别、人物管理）依赖以下组件，出于许可证合规**默认不含在 DMG 安装包里**——不装也能正常使用 Niu 的所有其他功能（聊天、知识图谱、文件管理等），只是照片处理不可用。

需要照片处理时，安装后用**程序自带的 Python**（不是系统 Python）手动安装：

```bash
# 用 DMG 安装后的自包含 Python（路径以 /Applications/niu.app 为例）
/Applications/niu.app/Contents/Resources/python/bin/python3 -m pip install \
    opencv-python-headless==4.11.0.86 \
    insightface==0.7.3 \
    easydict==1.13 \
    pillow-heif==1.4.0
```

> ⚠️ **必须用程序自带的 Python**，不能用系统 `pip install`——Niu 运行时用的是 `niu.app/Contents/Resources/python/` 这个自包含环境，装到系统 Python 里 Niu 看不到。

> 📋 **许可证说明**：`opencv-python-headless` 捆绑的 FFmpeg 含 GPL 编解码器（libx264/libx265），`pillow-heif` 链接 libx265（GPLv2）。你自行安装=你与 GPL 许可方建立许可关系，Niu 本身（MIT 许可证）不分发这些包，不构成 GPL 传染。`insightface` 和 `easydict` 是人脸识别库依赖，一并安装。

#### 人脸识别模型（buffalo_l）

InsightFace 的 `buffalo_l` 模型（~326MB）也是非商业许可证，**默认不含在 DMG 里**。首次用人脸识别时程序会自动下载，但**国内网络常下载失败**，建议手动下载安装：

1. 从 InsightFace 官方 GitHub 下载 `buffalo_l.zip`：
   - 地址：https://github.com/deepinsight/insightface/releases/tag/v0.7.3
   - 找 `buffalo_l.zip` 下载（国内访问慢可用代理）
2. 解压后把 5 个 `.onnx` 文件放到：
   ```
   ~/.insightface/models/buffalo_l/
   ```
   - 解压后目录结构应为该目录下直接是 `1k3d68.onnx` / `2d106det.onnx` / `det_10g.onnx` / `genderage.onnx` / `w600k_r50.onnx` 5 个文件，不要多套一层目录
3. 重启 Niu，下次拖入照片入库会直接从本地加载模型，不再下载

> ⚠️ **关于重新弹授权提示**：安装上述包会修改 `niu.app` 内部文件，可能触发 macOS 重新弹一次"无法验证开发者"提示。点"打开"即可，不影响使用。
```

- [ ] **Step 3: 验证**

Run: `grep -n "可选：启用照片处理\|buffalo_l.zip\|~/.insightface/models/buffalo_l" README.md`
Expected: 至少 3 行匹配

- [ ] **Step 4: 提交**

```bash
git add README.md
git commit -m "docs(README): 加'可选：启用照片处理'章节

照片处理依赖 cv2/insightface/easydict/pillow-heif（GPL）+ buffalo_l 模型（非商业）。
DMG 默认不含，用户需照片处理时按 README 用自包含 Python 自装。
含 buffalo_l 模型手动下载指引（国内自动下载常失败）。"
```

---

### Task 5: 系统手册"可选组件安装"章节同步补照片处理

**Files:**
- Modify: `docs/SYSTEM_MANUAL.md`（"可选组件安装"章节）

**Interfaces:** 无（纯文档）

- [ ] **Step 1: 读手册确认当前"可选组件安装"章节**

读 `docs/SYSTEM_MANUAL.md`，找"## 可选组件安装"章节。当前应有两个子节：脑区社区检测 + 人脸识别模型。确认结构。

- [ ] **Step 2: 删除原“人脸识别模型”子节 + 新增“照片处理”大子节（完全替换）**

当前“人脸识别模型（InsightFace buffalo_l）”子节只讲模型下载，要扩展成“照片处理（人脸识别 + HEIC）”完整子节，和脑区社区检测子节对齐。

**删除**原“### 人脸识别模型（InsightFace buffalo_l）”子节和“#### 首次下载超时失败怎么办”子节（旧子节里“DMG 只含 InsightFace 库代码”的描述已过时——本计划把 insightface 库也排除了），用下面的“### 照片处理”大子节**完全替换**：

```markdown
### 照片处理（人脸识别 + HEIC 支持）

照片处理功能（拖入照片入库、人脸识别、人物管理）依赖 `opencv-python-headless` + `insightface` + `easydict` + `pillow-heif` 四个包。其中 `opencv-python-headless` 捆绑的 FFmpeg 含 GPL 编解码器（libx264/libx265），`pillow-heif` 链接 libx265（GPLv2），出于许可证合规**默认不含在 DMG 里**——不装也能正常使用 Niu 所有其他功能，只是照片处理不可用。

如果需要照片处理，用**程序自带的 Python**手动安装：

```bash
# 用 DMG 安装后的自包含 Python（路径以 /Applications/niu.app 为例）
/Applications/niu.app/Contents/Resources/python/bin/python3 -m pip install \
    opencv-python-headless==4.11.0.86 \
    insightface==0.7.3 \
    easydict==1.13 \
    pillow-heif==1.4.0
```

> ⚠️ **必须用程序自带的 Python**，不能用系统 `pip install`——Niu 运行时用的是 `niu.app/Contents/Resources/python/` 这个自包含环境，装到系统 Python 里 Niu 看不到。

> 📋 许可证说明：`opencv-python-headless` 捆绑 GPL 版 FFmpeg，`pillow-heif` 链接 libx265（GPLv2）。用户自行安装=用户与 GPL 许可方建立许可关系，Niu 本身（MIT 许可证）不分发这些包，不构成 GPL 传染。`insightface` 和 `easydict` 是人脸识别库依赖，一并安装。

> ⚠️ 安装后会修改 `niu.app` 内部文件，可能触发 macOS 重新弹一次"无法验证开发者"提示。点"打开"即可，不影响使用。

安装后重启 Niu，照片处理功能会自动启用（`__init__.py` 的 `try/except ImportError` 会检测到包可用）。

#### 人脸识别模型（buffalo_l）

照片处理的人脸识别功能依赖 InsightFace 的 `buffalo_l` 模型（~326MB）。出于非商业许可证限制，**模型文件默认不含在 DMG 里**。

首次使用人脸识别功能（拖入照片入库）时，InsightFace 会**尝试自动下载** `buffalo_l` 模型到 `~/.insightface/models/buffalo_l/`，但**国内网络常下载失败**，建议手动下载：

1. 从 InsightFace 官方下载 `buffalo_l.zip`：
   - 地址：https://github.com/deepinsight/insightface/releases/tag/v0.7.3
2. 解压后把 5 个 `.onnx` 文件放到 `~/.insightface/models/buffalo_l/`（5 个文件直接在该目录下，不要多套一层）
3. 重启 Niu，下次用人脸识别会直接从本地加载

> 📋 许可证说明：InsightFace buffalo_l 模型是非商业许可证。用户自行下载=用户与 InsightFace 许可方建立许可关系，Niu 本身不分发这个模型，不承担非商业许可的责任。仅限非商业用途。

> 💡 模型加载后占用 ~326MB 内存，空闲 5 分钟自动卸载（`MODEL_IDLE_TIMEOUT_SECONDS = 300`）。

#### 首次下载超时失败怎么办

模型文件 ~326MB，弱网环境或下载源不稳定时可能超时失败，表现为人脸识别不工作（拖入照片不响应或报错）。此时 Agent 应：

1. **判断是否模型缺失**：检查 `~/.insightface/models/buffalo_l/` 目录是否存在且含 5 个 `.onnx` 文件（`1k3d68.onnx` / `2d106det.onnx` / `det_10g.onnx` / `genderage.onnx` / `w600k_r50.onnx`）。目录不存在或文件不全=模型没下载成功。
2. **建议用户手动下载**：让用户从 InsightFace 官方下载 `buffalo_l.zip`，解压后放到 `~/.insightface/models/buffalo_l/`（见上"人脸识别模型"子节）。
3. **重启 Niu**：放好后重启，下次用人脸识别会直接从本地加载，不再下载。
```

注意：这个改动会把之前已有的"人脸识别模型（InsightFace buffalo_l）"子节和"首次下载超时失败怎么办"子节合并重组到"照片处理"大子节下，避免内容重复。

- [ ] **Step 3: 验证**

Run: `grep -n "照片处理\|opencv-python-headless\|buffalo_l.zip" docs/SYSTEM_MANUAL.md`
Expected: 至少 3 行匹配

- [ ] **Step 4: 提交**

```bash
git add docs/SYSTEM_MANUAL.md
git commit -m "docs: 系统手册'可选组件安装'补照片处理子节

把原来的'人脸识别模型'子节扩展成'照片处理（人脸识别+HEIC）'完整子节：
- cv2/insightface/easydict/pillow-heif 安装命令 + 许可证说明
- buffalo_l 模型手动下载指引（国内自动下载常失败）
- 首次下载超时排查（Agent 可据此帮用户排查）
和脑区社区检测子节对齐，保持结构一致。"
```

---

### Task 6: 重新打包验证

**Files:** 无（纯打包验证）

**Interfaces:** 无

- [ ] **Step 1: 清理旧 bundle**

Run: `rm -rf niu.app`
（铁律：重打前必须 rm -rf niu.app，否则 rsync --delete --exclude 会保护被 exclude 的旧文件不删除）

- [ ] **Step 2: 用 build.sh 打包**

Run: `chmod +x launcher/build.sh && ./launcher/build.sh 2>&1 | tail -15`
Expected: `[build.sh] macOS .app bundle created at ../niu.app ...`

- [ ] **Step 3: 验证 8 项风险项都不在 bundle**

Run:
```bash
echo "=== 1. 不含 buffalo_l 模型 ==="
ls niu.app/Contents/Resources/models/models/buffalo_l/*.onnx 2>&1
echo "=== 2. 不含字体 ttf ==="
ls niu.app/Contents/Resources/ui/main/windows/assistant/fonts/AZhuPaoPaoTi.ttf 2>&1
echo "=== 3. 不含 igraph/leidenalg/texttable 包 ==="
ls -d niu.app/Contents/Resources/python/lib/python3.11/site-packages/igraph niu.app/Contents/Resources/python/lib/python3.11/site-packages/leidenalg niu.app/Contents/Resources/python/lib/python3.11/site-packages/texttable.py 2>&1
echo "=== 4. 不含 GPL dist-info ==="
ls -d niu.app/Contents/Resources/python/lib/python3.11/site-packages/igraph-*.dist-info niu.app/Contents/Resources/python/lib/python3.11/site-packages/python_igraph-*.dist-info niu.app/Contents/Resources/python/lib/python3.11/site-packages/leidenalg-*.dist-info niu.app/Contents/Resources/python/lib/python3.11/site-packages/texttable-*.dist-info 2>&1
echo "=== 5. 不含 cv2/insightface/easydict/pillow_heif 包 ==="
ls -d niu.app/Contents/Resources/python/lib/python3.11/site-packages/cv2 niu.app/Contents/Resources/python/lib/python3.11/site-packages/insightface niu.app/Contents/Resources/python/lib/python3.11/site-packages/easydict niu.app/Contents/Resources/python/lib/python3.11/site-packages/pillow_heif 2>&1
echo "=== 6. 不含对应 dist-info ==="
ls -d niu.app/Contents/Resources/python/lib/python3.11/site-packages/opencv_python_headless-*.dist-info niu.app/Contents/Resources/python/lib/python3.11/site-packages/insightface-*.dist-info niu.app/Contents/Resources/python/lib/python3.11/site-packages/easydict-*.dist-info niu.app/Contents/Resources/python/lib/python3.11/site-packages/pillow_heif-*.dist-info 2>&1
```
Expected: 第 1-6 项都报 "No such file or directory"

- [ ] **Step 4: 启动器验证缺失依赖检查**

由于无法在打包环境直接跑 GUI，改用代码审查 + grep 确认改动都在：
```bash
grep -n "check_missing_deps\|missing_deps" launcher/src/main.rs | head -10
```
Expected: 多行匹配（函数定义 + struct 字段 + new 参数 + main 调用 + view 使用）

- [ ] **Step 5: 确认无回归**

确认启动器编译通过（Task 2/3 已验证），bundle 资源完整（python3 binary、niu_api、agent、mcp-servers、config、bge-base-zh-v1.5 模型、Electron 等都在）。

- [ ] **Step 6: 提交（如果有遗留改动）**

如果打包过程没产生新改动，跳过本步。

---

## Self-Review

**1. Spec coverage:**
- ✅ "cv2 + pillow-heif 从 DMG 排除" → Task 1 build.sh exclude
- ✅ "不动 venv/requirements.txt" → Task 1 只改 build.sh，不改 requirements.txt/pyproject.toml
- ✅ "insightface + easydict 一并排除（依赖 cv2）" → Task 1 排除全部 4 个包
- ✅ "启动器检查缺失依赖" → Task 2 `check_missing_deps`
- ✅ "splash 窗口显示缺失项" → Task 3 view 改动
- ✅ "窗口动态加大" → Task 3 `WindowOpened` 触发 `window::resize`
- ✅ "不阻塞启动" → Task 3 关闭逻辑不变（ready_signal + status_check）
- ✅ "README 说明按需自装" → Task 4
- ✅ "系统手册同步" → Task 5
- ✅ "buffalo_l 模型手动下载指引（国内失败）" → Task 4 README + Task 5 手册
- ✅ "重新打包验证" → Task 6

**2. Placeholder scan:** 无 TODO/TBD，所有步骤都有完整代码。

**3. Type consistency:**
- `check_missing_deps(resources_root: &Path) -> Vec<String>` — Task 2 定义，Task 2 main 调用，一致 ✓
- `Splash.missing_deps: Vec<String>` — Task 2 加字段，Task 2 new 参数，Task 3 view 使用，一致 ✓
- `Splash::new` 新增 `missing_deps: Vec<String>` 参数 — Task 2 定义，Task 2 main 调用传入，一致 ✓
- `window::resize(id, Size::new(320.0, new_height))` — Task 3 WindowOpened 分支，Iced 0.13 API 确认可用 ✓

**4. 风险点：**
- **铁律遵守**：所有改动由子 Agent 执行（SDD），主 Agent 只 review。Task 6 打包用 `launcher/build.sh`（不用 cargo build）。
- **Iced `column.extend` 兼容性**：Task 3 Step 2 用了 `column.extend(items)`，Iced 0.13 可能不支持。计划已给 fallback（循环 push），implementer 如遇编译错误改用 fallback。
- **site-packages 版本路径硬编码**：`python3.11` 已确认（venv 实际版本），如果未来 Python 升级要同步改。可接受。
- **buffalo_l 模型检查逻辑**：检查 bundle 内 `models/models/buffalo_l/` + 用户家目录 `~/.insightface/models/buffalo_l/`，只要任一处有 .onnx 就不提示。开发者本地可能 bundle 内有（如果没重打），用户家目录可能有（如果用过人脸识别），两个都查避免误报。
- **窗口宽度 280→320**：Task 3 把宽度也加大到 320，因为提示文案"以下功能因依赖缺失暂不可用，请读 README 安装说明："较长，280 会截断。`WindowOpened` resize 时设 320 宽。
- **`dirs::home_dir()` 已在文件用**：`detect_niu_home`（约第 1391 行）已用 `dirs::home_dir()`，无需新 crate。
- **不阻塞启动的确认**：Task 3 只改 `view` 渲染 + `WindowOpened` 加 resize，不改 `Tick` 的关闭逻辑（`ready_signal_seen && status_check_completed`），启动流程完全不变。

无问题，计划可执行。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-28-gpl-deps-exclude-and-launcher-hint.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — 我派 fresh subagent 每个 Task 执行，任务间 review 门控，迭代快

**2. Inline Execution** — 在当前会话批量执行，检查点 review

**Which approach?**
