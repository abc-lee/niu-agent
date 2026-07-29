# 安装部署手册

> **定位**：从零到能运行的 Niu，覆盖下载安装、源码构建、macOS 打包。
>
> **不含**：启动后的配置操作（见用户操作手册）、开发期调试（见开发者参考）、依赖版本细节（见依赖与模型手册）。
>
> **何时查**：用户问"怎么装""怎么从源码跑""怎么打包发布""为什么某个组件不工作"时查这里。

---

## 一、下载安装（最终用户）

### 1.1 方式一：DMG 直接下载（推荐普通用户）

从 GitHub Release 下载最新的 `.dmg` 安装包：

| 平台 | 下载链接 | 说明 |
|------|---------|------|
| macOS（Intel CPU） | [Niu-0.1.0-mac-intel.dmg](https://github.com/abc-lee/niu-agent/releases/download/v0.1.0/Niu-0.1.0-mac-intel.dmg) | 适用于 Intel 芯片的 Mac（约 1.2 GB） |
| macOS（Apple M 系列） | _稍后提供_ | 后续发布 |

**安装步骤**：

1. 双击下载的 `.dmg` 文件挂载
2. 把挂载出来的 `niu.app` 拖到 `Applications` 文件夹
3. 首次启动时 macOS 会提示"无法验证开发者"——点"打开"授权一次即可（后续不再提示）
4. 启动后在设置中配置你的 LLM API Key

> 关于安全提示：本应用采用 ad-hoc 本地签名（无 Apple 开发者证书），首次启动的验证进度条和"无法验证开发者"提示是正常现象，点"打开"授权后系统会记住，后续直接启动。

### 1.2 可选组件安装

出于许可证合规，DMG 安装包默认不含以下两个组件，不影响 Niu 主体功能，只是对应子功能不工作。详见**系统手册**的"## 可选组件安装"章节：

- **脑区社区检测**（igraph + leidenalg，GPL）：缺包优雅降级，手动安装命令见主手册
- **人脸识别模型**（InsightFace buffalo_l，非商业）：首次用人脸识别时自动下载到 `~/.insightface/`

---

## 二、从源码构建（开发者）

### 2.1 前置环境

- **Python 3.11+**（用于 Agent 和 MCP 服务器）
- **Rust 工具链**（用于启动器，见第四章）
- **Node.js LTS**（用于 Electron 前端，建议 20.x）
- **SQLite**（用于会话持久化）

> Python 依赖版本约束（numpy<2 + opencv<4.12 等隐性约束）详见**依赖与模型手册**，此处不重复。

### 2.2 创建自包含 Python 运行时

```bash
# 1. 克隆仓库
git clone <repo-url>
cd ai-bot

# 2. 创建自包含 Python 运行时（venv + 全量依赖）
python3.11 -m venv --copies python
python/bin/pip install --upgrade pip
python/bin/pip install -r requirements.txt
```

**关于 `--copies`**：确保 venv 内二进制是真实拷贝（非符号链接指向系统 Python），便于打包分发。这是铁律——`python/` 必须完整自包含，禁止符号链接指向外部路径。

**依赖说明**：
- `requirements.txt` 含 Agent 核心 + 所有 MCP 服务器 + lightrag-hku（从 Fork git+https 安装）的完整依赖
- 测试依赖（pytest 等）单独维护在 `requirements-dev.txt`，不进入 `python/` 自包含运行时：
  ```bash
  python/bin/pip install -r requirements-dev.txt  # 仅开发时安装
  ```

最终用户**不需要**执行上述命令——他们拿到的 `niu.app` 已含完整 `python/` 运行时，开箱即用。

### 2.3 初始化用户数据目录

程序运行所需的用户数据模板位于 `memory/`：

| 文件 | 说明 |
|------|------|
| `memory.json` | 用户记忆（身份、偏好、工作目录） |
| `preferences.json` | 存储配置（分类、路径结构、冲突阈值） |
| `skills/*.md` | Skills 技能文件 |

首次运行时 Rust 启动器会自动复制到用户家目录：
- Linux/Mac: `~/.niu/`
- Windows: `%USERPROFILE%\.niu\`

> 仅当 `~/.niu/` 下对应文件不存在时才复制，避免覆盖用户已有的配置和记忆。手动复制命令详见**用户操作手册** 1.1 首次启动流程。

### 2.4 编译并启动 Rust 启动器

```bash
cd launcher && cargo run --release
```

启动后在 `config/user-config.json` 中配置 LLM API Key 即可开始使用。支持的模型预设见 `config/llm-presets.json`，本地模型（向量模型、人脸识别模型）会在首次使用时自动从 `models/` 目录加载，无需手动下载。

> 开发期调试技巧（日志位置、SSE 事件追踪）详见**开发者参考** 1.1。

---

## 三、macOS .app 打包（发布者）

最终用户拿到的 macOS 安装包是 `niu.app`（含完整运行时：Rust 启动器 + 自包含 Python + Electron 前端 + 模型 + 配置模板）。打包流程全部由 `launcher/build.sh` 自动完成。

> ⚠️ **铁律**：Rust 启动器编译必须用 `launcher/build.sh`，禁止直接 `cargo build`——`cargo build` 只输出到 `launcher/target/debug/`，不会复制到项目根目录的 `niu`，导致测试用的是旧二进制。`build.sh` 编译后自动 `cp target/release/niu-launcher ../niu`。

### 3.1 前置条件

1. **python/ 自包含运行时已就位**（含 stdlib + dylib + Resources stub，install_name_tool 已改）
2. **ui/main/node_modules 已安装**（Electron 33 + 依赖）
3. **Rust 工具链已安装**（含 cargo + rustup target，见第四章）

### 3.2 打包命令

```bash
cd launcher
./build.sh
```

`build.sh` 会自动完成：
1. `cargo build --release` 编译 Rust 启动器
2. 复制二进制到项目根 `niu`（开发模式裸二进制，命令行 `./niu` 用）
3. macOS 下额外构造 `niu.app/Contents/MacOS/niu`（Finder 双击用）
4. 复制运行时资源到 `niu.app/Contents/Resources/`：
   - `python/`（自包含 Python 运行时，**排除 igraph/leidenalg/texttable**——GPL 依赖，用户按需手动安装）
   - `ui/main/`（Electron 前端，**排除阿朱泡泡体 ttf**——许可证存疑）
   - `config/`（配置模板，首次启动复制到 `~/.niu/config/`）
   - `models/`（向量模型 + 人脸识别模型，**排除 buffalo_l/*.onnx**——非商业许可，首次用自动下载到 `~/.insightface/`）
   - `memory/`（用户记忆模板）
   - `niu_api/`、`agent/`、`mcp-servers/`（Python 模块，PYTHONPATH 引用）
5. 调用 `scripts/relocate_python_framework.sh` 把 stdlib + dylib + Resources stub 复制到 `python/lib/`，并用 install_name_tool 改 dylib 引用为 `@rpath` 自包含
6. 逐个 codesign ad-hoc 签名（不用 `--deep`，自 macOS 13.3 起废弃）：
   - Python `.so` / `.dylib`（并行 4 进程）
   - `python3` 二进制
   - Electron 主二进制 + Helper + Framework + `.node`
   - 顶层 `niu.app`
7. `lsregister -f` 注册到 LaunchServices（打 `com.apple.provenance` xattr）
8. `xattr -w com.apple.quarantine` 主动加 quarantine（首次双击弹"无法验证开发者"对话框，用户点"打开"授权后系统记住）

> **打包注意事项**：重打 DMG 前必须先 `rm -rf niu.app`，否则旧 bundle 的 exclude 文件不会被删（rsync `--delete --exclude` 会保护被 exclude 的旧文件不删除）。详见许可证合规改造记录。

### 3.3 产物

| 路径 | 用途 |
|------|------|
| `niu` | 项目根裸二进制，命令行 `./niu` 启动（开发调试用） |
| `niu.app/` | macOS 应用包，Finder 双击启动（最终用户用） |

### 3.4 分发

把整个 `niu.app/` 目录拷贝到目标 Mac（同架构：x86_64 或 ARM64）的任意位置（如 `/Applications/`），双击即可运行。目标 Mac **不需要**安装 Python / Node.js / Rust 工具链——所有运行时都已自包含在 `niu.app/Contents/Resources/` 内。

首次双击会弹"无法验证开发者"对话框（因为使用 ad-hoc 签名），点"打开"授权后系统记住，后续直接启动。

### 3.5 生成 DMG 安装包

```bash
# 准备 DMG 拖拽安装目录
STAGE=/tmp/niu_dmg_stage
rm -rf "$STAGE" && mkdir -p "$STAGE"
ln -sf /Applications "$STAGE/Applications"      # Applications 软链，支持拖拽安装
cp -R niu.app "$STAGE/niu.app"

# 生成 DMG（UDZO = zlib 压缩，3.3G bundle → 约 1.2G DMG）
hdiutil create -volname "Niu" -srcfolder "$STAGE" -fs HFS+ -format UDZO -imagekey zlib-level=9 dist/Niu-0.1.0-mac-intel.dmg
rm -rf "$STAGE"
```

产物在 `dist/Niu-0.1.0-mac-intel.dmg`，可上传到 GitHub Release。

### 3.6 Info.plist 关键配置

| Key | 值 | 作用 |
|-----|----|----|
| `CFBundleIdentifier` | `com.niu.launcher` | bundle 唯一标识 |
| `CFBundleExecutable` | `niu` | 指向 `Contents/MacOS/niu` |
| `LSUIElement` | `true` | Accessory 模式：Dock 不显示图标，避免抢焦点；窗口仍可显示（启动器内部调 `activateIgnoringOtherApps:YES` 强制激活） |
| `LSMinimumSystemVersion` | `11.0` | 最低 macOS 版本 |

### 3.7 跨架构打包（M 系列 Mac）

`niu.app` 当前是**单架构**包（x86_64 或 arm64），不是 universal binary。原因：Python 扩展模块（`.so`/`.dylib`）和 Electron 二进制是 `pip install` / `npm install` 时按 host 架构自动选择的 wheel，PyPI 上 torch/insightface 等关键包没有 universal2 wheel，无法合并。

**当前包在 M 系列 Mac 上的运行情况**：
- x86_64 包在 M 系列 Mac 上**能运行**（通过 macOS 内置 Rosetta 2 转译）
- 但不是原生：torch（PyTorch 推理）和 onnxruntime（InsightFace 人脸识别）等计算密集型任务性能损失约 20-40%

**分发策略**：分别打包。Intel Mac 打 x86_64 包，M 系列 Mac 打 arm64 包。`build.sh` 和 `relocate_python_framework.sh` 都是 host-architecture-neutral 的——在哪种 Mac 上跑就打哪种架构的包，无需任何改造。

#### M 系列 Mac 上打包完整步骤

**核心约束**：`python/lib/python3.11/site-packages/*.so` 和 `ui/main/node_modules/electron` 必须在 M 系列 Mac 上 `pip install` / `npm install`，**不能在 Intel Mac 上 cross-compile**（pip/npm 会自动选 host 架构的 wheel，cross 会混入 x86_64 .so 导致运行时崩溃）。

**1. 安装开发环境（一次性）**

```bash
# Xcode Command Line Tools（git + clang + make）
xcode-select --install

# Rust 工具链
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"

# Node.js LTS（建议 20.x）
# 方式 A: brew install node@20
# 方式 B: 从 https://nodejs.org 下载 macOS arm64 安装包

# Python 3.11 universal2（必须用 python.org 的 universal2 安装包，禁止用 brew）
# brew 装的是单架构 arm64-only，没有 /Library/Frameworks/Python.framework/ 路径，
# scripts/relocate_python_framework.sh 会找不到 dylib 直接退出
# 浏览器打开 https://www.python.org/downloads/release/python-3110/
# 下载 "macOS 64-bit universal2 installer" 并安装
```

**2. 准备项目依赖**

```bash
# 克隆项目
git clone <项目仓库地址> ai-bot
cd ai-bot

# 创建 python/ venv（--copies 必须保留，生成真实二进制）
python3.11 -m venv --copies python

# 安装 Python 依赖（pip 自动选 arm64 wheel）
python/bin/pip install --upgrade pip
python/bin/pip install -r requirements.txt

# 安装 Electron + 前端依赖（npm 自动选 arm64 Electron）
cd ui/main
npm install
cd ../..

# 修复可执行权限（git 操作后必须执行）
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

**3. 打包**

```bash
./launcher/build.sh
```

`build.sh` 不需要任何参数或环境变量，自动用 host 的 arm64 架构编译 Rust + 复制资源 + 签名 + 注册 LaunchServices。

**4. 验证产物是 arm64**

```bash
# Rust 启动器必须是 arm64
file niu.app/Contents/MacOS/niu
# 期望: Mach-O 64-bit executable arm64

# 关键 Python .so 必须是 arm64（这是核心检查）
file niu.app/Contents/Resources/python/lib/python3.11/site-packages/numpy/core/_multiarray_umath.cpython-311-darwin.so
file niu.app/Contents/Resources/python/lib/python3.11/site-packages/torch/lib/libtorch_cpu.dylib
file niu.app/Contents/Resources/python/lib/python3.11/site-packages/onnxruntime/capi/onnxruntime_pybind11_state.cpython-311-darwin.so
# 期望: arm64

# Electron 主二进制必须是 arm64
file niu.app/Contents/Resources/ui/main/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron
# 期望: Mach-O 64-bit executable arm64

# Python 解释器和 libPython3.11.dylib 是 universal2 双架构是正常的
# （python.org universal2 installer 自带，不影响 arm64 原生运行）
```

#### 注意事项

- **不能用 `brew install python@3.11`**：brew 装的是单架构 arm64-only，没有 `/Library/Frameworks/Python.framework/` 路径，`scripts/relocate_python_framework.sh` 第 13-15 行硬编码了这个路径，brew Python 会让脚本直接退出
- **不能在 Intel Mac 上 cross-compile arm64 包**：虽然 `cargo build --target aarch64-apple-darwin` 能编译 Rust 部分，但 site-packages 的 .so 和 Electron 二进制必须在 arm64 host 上 `pip install` / `npm install` 才能拿到 arm64 wheel
- **`lightrag-hku` 从 GitHub 源码编译**：`requirements.txt` 里的 `lightrag-hku @ git+https://github.com/abc-lee/LightRAG.git` 在 M 系列 Mac 上会编译 arm64 扩展，需要联网能访问 GitHub
- **`torch==2.2.2` 有 arm64 wheel**：PyPI 上 `macosx_11_0_arm64` tag 可用，M 系列 Mac 上 pip 直接装，无需特殊处理

---

## 四、Rust 启动器编译

Rust 启动器需要根据目标平台编译对应架构的二进制：

```bash
cd launcher

# 当前平台原生编译
cargo build --release

# 编译产物位于 target/release/niu-launcher
# 将其复制到项目根目录即可
cp target/release/niu-launcher ../
```

**跨平台交叉编译**（需要先安装对应 target）：

| 目标平台 | 命令 |
|---------|------|
| macOS Intel (x86_64) | `rustup target add x86_64-apple-darwin && cargo build --release --target x86_64-apple-darwin` |
| macOS Apple Silicon (ARM64) | `rustup target add aarch64-apple-darwin && cargo build --release --target aarch64-apple-darwin` |
| Windows x86_64 | `rustup target add x86_64-pc-windows-msvc && cargo build --release --target x86_64-pc-windows-msvc` |
| Linux x86_64 | `rustup target add x86_64-unknown-linux-gnu && cargo build --release --target x86_64-unknown-linux-gnu` |

编译产物位于 `target/<target>/release/niu-launcher`（Windows 为 `niu-launcher.exe`）。

> **注意**：macOS 交叉编译需要 Xcode Command Line Tools。Windows 交叉编译需在 Windows 上执行或配置交叉工具链。
>
> **打包时必须用 `launcher/build.sh`**（见第三章），不能只用 `cargo build`——`build.sh` 会额外完成复制到根目录、构造 .app、签名、注册 LaunchServices 等步骤。

---

## 五、相关手册

| 关联主题 | 查哪本手册 |
|---------|-----------|
| 可选组件（igraph/leidenalg、人脸模型）详细安装/许可证 | **系统手册** "可选组件安装"章节 |
| Python 依赖版本约束、模型文件管理 | **依赖与模型手册** (manual-dependencies.md) |
| 首次启动后的 LLM 配置、用户操作 | **用户操作手册** (manual-user-guide.md) |
| 开发期调试技巧、API 端点、环境变量 | **开发者参考** (manual-developer.md) |
| 性能优化（InsightFace 内存、启动速度、GPU） | **性能优化手册** (manual-performance.md) |
