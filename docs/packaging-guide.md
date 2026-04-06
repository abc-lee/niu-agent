# 打包指南 - 完整流程

## 概述

本文档说明如何将 Niu 个人知识助理打包成**独立可执行文件**，无需任何外部依赖。

**核心原则：**
- ✅ 所有依赖打包进程序
- ✅ 无需运行时下载
- ✅ 国内网络友好
- ✅ 零配置安装

---

## 一、打包前置准备

### 1.1 目录结构

```
E:\tools\ai-bot\
├── requirements.txt           # Python 依赖列表
├── models/                    # 模型文件目录（打包前准备）
│   ├── buffalo_l/            # 人脸识别模型（326MB）
│   └── paraphrase-multilingual-MiniLM-L12-v2/  # 向量模型（466MB）
├── python_packages/          # Python 包缓存（打包前准备）
│   └── *.whl                # 所有依赖的 wheel 文件
└── scripts/
    └── package_all_dependencies.py  # 依赖打包脚本
```

### 1.2 下载所有依赖

**步骤 1：运行依赖打包脚本**

```bash
cd E:\tools\ai-bot
python scripts/package_all_dependencies.py
```

此脚本会：
1. 下载人脸识别模型（buffalo_l, 326MB）
2. 下载向量模型（paraphrase-multilingual-MiniLM-L12-v2, 466MB）
3. 下载所有 Python 包到 `python_packages/` 目录

**预计时间：** 15-30 分钟（取决于网速）
**预计大小：** ~1.3GB

**步骤 2：验证依赖完整性**

```bash
python scripts/package_all_dependencies.py  # 会自动验证
```

输出应显示：
```
✅ 人脸识别模型: models/buffalo_l
✅ 向量模型: models/paraphrase-multilingual-MiniLM-L12-v2
✅ Python 包: XXX 个 wheel 文件
✅ 所有依赖已准备就绪，可以打包
```

---

## 二、打包方式选择

### 方案 A：go-embed-python（推荐）

**优点：**
- 真正的单文件可执行
- Python 运行时嵌入到 Go 程序中
- 无需外部 Python 环境

**步骤：**

1. **安装 go-embed-python**
   ```bash
   go get github.com/kluctl/go-embed-python
   ```

2. **修改 main.go**
   ```go
   import (
       "github.com/kluctl/go-embed-python/python"
       "github.com/kluctl/go-embed-python/pip"
   )

   func main() {
       // 创建嵌入式 Python
       ep, err := python.NewEmbeddedPython("niu-assistant")
       if err != nil {
           panic(err)
       }
       defer ep.Cleanup()

       // 安装依赖
       err = pip.InstallPackages(ep, "python_packages/*.whl")
       if err != nil {
           panic(err)
       }

       // 启动 Python API
       exePath, _ := ep.GetExePath()
       cmd := exec.Command(exePath, "-m", "niu_api")
       // ...
   }
   ```

3. **生成嵌入式 Python**
   ```bash
   go generate ./...
   go build -o niu-assistant.exe
   ```

**参考：** `docs/feature-one-click-install.md`

---

### 方案 B：PyInstaller（备选）

**优点：**
- 成熟稳定
- 打包简单

**缺点：**
- 需要手动处理模型文件
- 生成多个文件

**步骤：**

1. **安装 PyInstaller**
   ```bash
   pip install pyinstaller
   ```

2. **创建 spec 文件**
   ```python
   # niu_api.spec
   a = Analysis(
       ['niu_api/__main__.py'],
       pathex=['.'],
       binaries=[],
       datas=[
           ('models', 'models'),  # 包含模型文件
           ('config', 'config'),  # 包含配置文件
       ],
       hiddenimports=[
           'insightface',
           'onnxruntime',
           'sentence_transformers',
           # ... 所有依赖
       ],
       hookspath=[],
       hooksconfig={},
       runtime_hooks=[],
       excludes=[],
       win_no_prefer_redirects=False,
       win_private_assemblies=False,
       cipher=None,
       noarchive=False,
   )
   pyz = PYZ(a.pure, a.zipped_data, cipher=None)

   exe = EXE(
       pyz,
       a.scripts,
       a.binaries,
       a.zipfiles,
       a.datas,
       [],
       name='niu-assistant',
       debug=False,
       bootloader_ignore_signals=False,
       strip=False,
       upx=True,
       upx_exclude=[],
       runtime_tmpdir=None,
       console=True,
       disable_windowed_traceback=False,
       argv_emulation=False,
       target_arch=None,
       codesign_identity=None,
       entitlements_file=None,
   )
   ```

3. **打包**
   ```bash
   pyinstaller niu_api.spec
   ```

---

## 三、模型文件处理

### 3.1 模型加载策略

**优先级：**
```
1. 环境变量 NIU_MODELS_PATH
2. 程序目录下的 models/
3. 用户目录 ~/.niu/models/
```

**代码修改：**

```python
# niu_api/internal/embedding.py
def get_models_dir() -> Path:
    """Get models directory path."""
    # 1. 环境变量（最高优先级）
    if "NIU_MODELS_PATH" in os.environ:
        return Path(os.environ["NIU_MODELS_PATH"])

    # 2. 程序目录下的 models/（打包后的位置）
    exe_dir = Path(sys.executable).parent
    local_models = exe_dir / "models"
    if local_models.exists():
        return local_models

    # 3. 开发环境
    dev_models = Path(__file__).parent.parent.parent.parent / "models"
    if dev_models.exists():
        return dev_models

    # 4. 用户目录（降级）
    return Path.home() / ".niu" / "models"
```

### 3.2 模型打包位置

**Windows 打包：**
```
niu-assistant.exe
├── niu-assistant.exe          # 主程序
├── models/                    # 模型文件
│   ├── buffalo_l/
│   └── paraphrase-multilingual-MiniLM-L12-v2/
├── config/                    # 配置文件
└── python/                    # 嵌入式 Python（可选）
```

---

## 四、完整打包流程

### 4.1 开发环境准备

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 下载模型和依赖包
python scripts/package_all_dependencies.py

# 3. 验证
python scripts/package_all_dependencies.py  # 会验证
```

### 4.2 执行打包

**使用 go-embed-python：**
```bash
# 1. 生成嵌入式 Python
go generate ./...

# 2. 编译
go build -ldflags="-s -w" -o niu-assistant.exe

# 3. 复制模型文件和文档
cp -r models/ niu-assistant-models/
cp -r docs/ niu-assistant-docs/

# 4. 创建安装包
# 使用 NSIS 或 Inno Setup
```

### 4.3 首次启动配置

**重要：首次启动后必须注入系统说明书！**

```bash
# 1. 启动程序
.\niu-assistant.exe

# 2. 等待预加载完成（约 25 秒）

# 3. 注入系统说明书到向量库（新终端）
python scripts/inject_system_manual.py

# 输出应显示：
# ✅ L1 摘要已注入
# ✅ L2 原文已注入
# ✅ 系统说明书注入完成
```

**为什么需要注入？**
- 系统说明书包含完整的技术文档和故障排查指南
- 注入到向量库后，主Agent可通过语义检索自动访问
- 用户提出问题（如"启动慢"、"人脸识别问题"）时，主Agent会自动找到相关章节并提供解决方案

### 4.4 测试打包结果

```bash
# 在干净的环境中测试
# 1. 临时移除 Python 环境变量
set PYTHONPATH=
set PATH=%PATH:C:\Python311;=%

# 2. 运行程序
.\niu-assistant.exe

# 3. 验证功能
# - 启动正常
# - 模型加载正常
# - 人脸识别正常
# - 向量搜索正常

# 4. 验证系统说明书注入
# 在对话中输入："启动很慢怎么办？"
# 主Agent应能检索到系统说明书并提供解决方案
```

---

## 五、安装包制作

### 5.1 使用 NSIS（Windows）

```nsis
; niu-installer.nsi
!define APP_NAME "Niu 个人知识助理"
!define APP_VERSION "0.2.0"

Name "${APP_NAME}"
OutFile "niu-assistant-setup.exe"
InstallDir "$PROGRAMFILES64\${APP_NAME}"

Section "Install"
    SetOutPath $INSTDIR

    ; 主程序
    File "niu-assistant.exe"

    ; 模型文件
    File /r "models"

    ; 配置文件
    File /r "config"

    ; 创建快捷方式
    CreateDirectory "$SMPROGRAMS\${APP_NAME}"
    CreateShortCut "$SMPROGRAMS\${APP_NAME}\${APP_NAME}.lnk" "$INSTDIR\niu-assistant.exe"

    ; 注册卸载信息
    WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
    Delete "$INSTDIR\*.*"
    RMDir /r "$INSTDIR\models"
    RMDir /r "$INSTDIR\config"
    Delete "$SMPROGRAMS\${APP_NAME}\*.*"
    RMDir "$SMPROGRAMS\${APP_NAME}"
    RMDir "$INSTDIR"
SectionEnd
```

### 5.2 安装包大小

**压缩前：**
- 主程序：~50MB
- 模型文件：792MB
- 总计：~842MB

**压缩后（7z/NSIS）：**
- 安装包：~500MB

---

## 六、分发与更新

### 6.1 分发渠道

1. **GitHub Releases**（推荐）
   - 提供 Windows/macOS/Linux 版本
   - 自动更新检查

2. **网盘分发**
   - 百度网盘
   - 阿里云盘
   - 蓝奏云

### 6.2 自动更新

使用 `electron-updater`（前端）+ `goupdate`（后端）：

```go
import "github.com/inconshreveable/go-update"

func checkUpdate() {
    // 检查更新
    resp, _ := http.Get("https://api.niu.ai/version")
    // ...
}
```

---

## 七、常见问题

### Q1: 打包后模型加载失败？

**A:** 检查模型路径优先级：
```python
# 在 get_models_dir() 中添加调试日志
print(f"[DEBUG] Models dir: {models_dir}")
print(f"[DEBUG] Exists: {models_dir.exists()}")
```

### Q2: 打包后 Python 包缺失？

**A:** 确保 `requirements.txt` 包含所有依赖，并运行打包脚本。

### Q3: GPU 版本如何打包？

**A:** GPU 版本不打包，用户可选安装：
```bash
# 用户自行安装 GPU 版本
pip install onnxruntime-gpu
```

---

## 八、检查清单

打包前确认：

- [ ] 已运行 `package_all_dependencies.py`
- [ ] `models/buffalo_l` 存在且完整（4 个 .onnx 文件）
- [ ] `models/paraphrase-multilingual-MiniLM-L12-v2` 存在
- [ ] `python_packages/` 包含所有 wheel 文件
- [ ] `requirements.txt` 包含所有依赖
- [ ] `docs/SYSTEM_MANUAL.md` 存在（系统说明书）
- [ ] 测试环境无 Python 环境变量
- [ ] 打包后测试所有功能
- [ ] 首次启动后注入系统说明书

---

**文档版本：** v1.0
**最后更新：** 2026-04-06
