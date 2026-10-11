# 依赖与模型手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，包含依赖管理和模型文件的详细指引。
> 如需系统概述和架构信息，请参阅 [SYSTEM_MANUAL.md](SYSTEM_MANUAL.md)。

## 一、依赖管理

### 1.1 Python 依赖

#### 关键版本约束（铁律）

以下 5 条版本约束来自 `requirements.txt` 顶部，**违反任意一条都会导致崩溃**：

| 序号 | 约束 | 原因 |
|------|------|------|
| 1 | `numpy<2` | torch 2.2.2 和 insightface 的 C 扩展用 NumPy 1.x 编译，numpy 2.x 会崩溃 |
| 2 | `torch==2.2.2` | 项目固定版本，不要升级；GPU 能力由 `get_device()` **运行时检测**，不是"没装就用 CPU" |
| 3 | `transformers>=4.41,<5.0` | transformers 5.x 要求 torch>=2.4，与 torch 2.2.2 不兼容 |
| 4 | `huggingface_hub<1` | 1.x 与 sentence-transformers 不兼容 |
| 5 | `lightrag-hku` 必须从 Fork 安装 | `git+https://github.com/abc-lee/LightRAG.git`，不能用 PyPI 版本（缺 PR#2990 修复） |

> 2026-10-11 **曾一度摘除 torch / transformers / sentence-transformers / accelerate，
> 当日即回滚**。回滚理由（用户纠正，PM 原论据有误）：本仓是**自包含完整 Python 运行时**，
> 依赖必须全部装进 bundle；**GPU / CPU 由运行时按机器能力判断**（`get_device()` 检测
> `torch.cuda`），不是"没装就用 CPU"——摘依赖会让有 GPU 的机器被静默降级；
> 且 macOS 打包后不能再补装依赖（触发重签名）。
> 另：reranker 本期已停用（见 `lightrag_manager.py` 的 `reranker_func = None`），
> 但**停用 reranker 不是保留依赖的理由**，保留依赖的理由就是上面几条。
> 向量模型本身已走 llama.cpp + GGUF，这一层与 torch 无关。

#### 核心依赖（agent 核心）

| 包名 | 版本 | 用途 |
|------|------|------|
| `litellm` | ==1.88.1 | LLM 统一接口 |
| `mcp` | ==1.27.1 | MCP 协议 |
| `aiosqlite` | ==0.22.1 | 异步 SQLite |
| `pydantic` | ==2.13.4 | 数据验证 |
| `httpx` | ==0.28.1 | HTTP 客户端 |
| `loguru` | ==0.7.3 | 日志 |
| `watchdog` | ==6.0.0 | 文件监控 |

**API 服务依赖（niu_api）：**

| 包名 | 版本 | 用途 |
|------|------|------|
| `fastapi` | ==0.136.1 | Web 框架 |
| `uvicorn` | ==0.47.0 | ASGI 服务器 |
| `numpy` | ==1.26.4 | 数值计算 |
| `nano-vectordb` | ==0.0.4.3 | 轻量向量库（LightRAG 同进程默认后端） |
| `sentence-transformers` | ==5.5.1 | 依赖须随 bundle 安装（GPU/CPU 运行时判断）；reranker 本期停用但模块保留 |

**人脸识别依赖（photo-server）：**

| 包名 | 版本 | 用途 |
|------|------|------|
| `insightface` | >=0.7.3 | 人脸识别 |
| `onnxruntime` | ==1.23.2 | ONNX 推理（CPU） |
| `opencv-python-headless` | >=4.8.0 | 图像处理（无 GUI） |
| `Pillow` | >=10.0.0 | 图像操作 |

**可选依赖：**

| 包名 | 用途 | 说明 |
|------|------|------|
| `onnxruntime-gpu` | CUDA GPU 加速 | 需要 NVIDIA GPU + CUDA |
| `onnxruntime-directml` | Windows GPU 加速 | 无需 CUDA，但仅 Windows |

**知识图谱依赖（LightRAG fork）：**

```text
lightrag-hku @ git+https://github.com/abc-lee/LightRAG.git
```

**禁止使用 PyPI 官方版本**：fork 版本包含 PR#2990 修复（`explore_node` 方向/边类型过滤，避免高连接度实体输出爆炸），PyPI 官方版缺此修复。安装命令：`pip install git+https://github.com/abc-lee/LightRAG.git`。

### 1.2 GPU 支持策略

**自动检测优先级：**
```python
CUDAExecutionProvider      # NVIDIA GPU（需 CUDA）
DmlExecutionProvider       # Windows DirectML（无需 CUDA）
CPUExecutionProvider       # CPU（默认，所有用户可用）
```

**检测逻辑：**
```python
# mcp-servers/photo-server/src/niu_photo_server/__init__.py

def _detect_available_providers() -> list[str]:
    """自动检测可用的 ONNX Runtime ExecutionProvider"""
    import onnxruntime as ort
    available = ort.get_available_providers()

    # 按优先级排序
    priority = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]

    selected = []
    for provider in priority:
        if provider in available:
            selected.append(provider)

    # 如果没有找到任何优先 provider，使用所有可用的
    if not selected:
        selected = available

    return selected
```

**ctx_id 选择注意：**

InsightFace 的 `ctx_id` 参数仅对 CUDA 有效：`ctx_id=0` 表示使用 GPU，`ctx_id=-1` 表示 CPU。代码中仅当 `CUDAExecutionProvider` 在 providers 列表中时才设 `ctx_id=0`，DirectML 仍使用 `ctx_id=-1`（ONNX Runtime 通过 DirectML provider 加速，InsightFace 内部走 CPU 路径）。

**用户场景：**

| 用户环境 | 自动选择 | 性能 |
|---------|---------|------|
| 有 NVIDIA GPU + CUDA | CUDA | 最快（10倍加速） |
| Windows + 任意 GPU | DirectML | 快（3倍加速） |
| 无 GPU 或未安装 CUDA | CPU | 慢但可用 |

### 1.3 模型加载时机

**启动时预加载（仅模块代码，不加载模型）：**
```
[PRELOAD] Importing cv2...
[PRELOAD] Importing InsightFace...
[PRELOAD] Pre-load complete
```

**为什么必须预加载？**
- 同进程架构下，InsightFace 在启动时预加载模块代码，避免运行时加载延迟
- 预加载模块代码（不是模型），确保首次人脸识别时快速响应

**模型按需加载（首次使用时）：**
```
用户拖入照片 → get_face_model() → 加载 buffalo_l（326MB） → 人脸识别
```

**模型自动卸载（空闲 5 分钟）：**
```
无人脸识别操作 5分钟 → 自动卸载模型 → 释放 326MB 内存
```

卸载由后台守护线程定期检查（每 60 秒），不调用 `gc.collect()` 避免 ONNX Runtime 崩溃。

### 1.4 Rust 原生依赖（niu-natives）

桌面自动化原生扩展（屏幕/窗口/区域抓图 + 输入注入 + 多屏坐标映射 + Windows UIA 语义树），PyO3 绑定，maturin 构建 wheel 装入 `python/` 环境；既服务 MCP `vision-server` 的 `screenshot` / `list_targets` 工具，也是内置 `computer` 工具（对象模型桌面操作）的底座。

**来源：** oh-my-pi（`https://github.com/can1357/oh-my-pi`，MIT）的 `crates/pi-natives/src/desktop` 模块，移植为仓内独立 crate `niu-natives/`（自带 Cargo.lock 提交；`niu-natives/LICENSE` = omp MIT 三版权行 + Niu 修改声明）。

**关键 crate 与许可证**（与 `niu-natives/Cargo.toml` 声明一一对应；全量 303 项含传递依赖见根目录 `THIRD-PARTY-NOTICES.txt`，零 GPL/LGPL/AGPL）：

| Crate | 版本 | 许可证 | 用途 |
|------|------|------|------|
| `pyo3` | 0.29.2 | MIT OR Apache-2.0 | Python 绑定（extension module） |
| `image` | 0.25.10 | MIT OR Apache-2.0 | PNG 编解码 |
| `flume` | 0.11.1 | MIT OR Apache-2.0 | 结果通道（并发原语） |
| `parking_lot` | 0.12.5 | MIT OR Apache-2.0 | 锁（并发原语） |
| `xcap` | 0.9.6 | Apache-2.0 | 屏幕/窗口采集（macOS+Windows） |
| `enigo` | 0.6.1 | MIT | 输入注入（Windows；macOS 纯 CGEvent） |
| `core-graphics` | 0.25.0 | MIT OR Apache-2.0 | macOS 图形 API（CGEvent 输入） |
| `objc2` 系 | objc2 0.6.4；app-kit / core-graphics / foundation / application-services / core-foundation 均 0.3.2 | Zlib/Apache-2.0/MIT（objc2 为 MIT，其余 5 个三许可、源码无 license 文件——见清单 Part 2） | macOS 系统 API（`application-services` Processes feature：`TransformProcessType` 后台降级） |
| `foreign-types` | 0.5.0 | MIT/Apache-2.0 | CF/CG FFI 对象封装（macOS） |
| `libc` | 0.2.189 | MIT OR Apache-2.0 | C ABI（pid 类型、私有框架动态加载/符号查找） |
| `tempfile` | 3.27.0 | MIT OR Apache-2.0 | macOS 采集路径临时文件 |
| `windows-sys` | 0.61.2 | MIT OR Apache-2.0 | Windows 系统 API |
| `uiautomation` | 0.25.0 | Apache-2.0（源码无 license 文件——见清单 Part 2） | Windows UIA 语义树（`desktop/ax.rs` Uia 变体） |
| `pyo3-build-config` | 0.29.2 | MIT OR Apache-2.0 | 构建依赖：macOS 扩展模块链接参数 |

**构建方式**（非 pip 安装，从仓内源码构建；`maturin` 为开发依赖见 `requirements-dev.txt`，打包流程 `launcher/build.sh` 已内嵌同步骤）：

```bash
cd niu-natives && maturin build --release -i ../python/bin/python
cd .. && python/bin/pip install --force-reinstall niu-natives/target/wheels/niu_natives-*.whl
```

> 以上为 macOS 形态（`python/bin/`）；Windows 命令见 README「编译 niu-natives」章节（路径形态为 `python\Scripts\`，且须用 `for %f in (...)`，不能照抄 Unix glob）。

**许可证归集：** 根目录 `THIRD-PARTY-NOTICES.txt`（cargo vendor 全量提取，含 Windows-only 与跨平台传递依赖；打包时复制进 `.app` Resources/）。

---

## 二、模型文件

### 2.1 人脸识别模型 (buffalo_l)

**模型信息：**

| 属性 | 值 |
|------|-----|
| **名称** | buffalo_l |
| **来源** | InsightFace v0.7 |
| **大小** | ~326MB |
| **包含** | SCRFD-10GF（检测）+ ResNet50（识别）+ 关键点 + 性别年龄 |
| **许可** | 非商业研究用途 |

**文件列表：**

```
models/models/buffalo_l/
├── det_10g.onnx          # 人脸检测（~16MB）
├── w600k_r50.onnx        # 人脸识别（~166MB）
├── 2d106det.onnx         # 2D 关键点（~5MB）
├── 1k3d68.onnx           # 3D 关键点（~137MB）
└── genderage.onnx        # 性别年龄（~1MB）
```

**加载逻辑：**

```python
# mcp-servers/photo-server/src/niu_photo_server/__init__.py

def get_face_model():
    """加载人脸识别模型"""
    models_dir = get_models_dir()  # 优先本地目录
    local_model_path = models_dir / "models" / "buffalo_l"

    providers = _detect_available_providers()  # 自动检测 GPU/CPU

    _face_model = FaceAnalysis(
        name="buffalo_l",
        root=str(models_dir),
        providers=providers
    )
    # ctx_id: 0 = CUDA GPU, -1 = CPU/DirectML
    ctx_id = 0 if "CUDAExecutionProvider" in providers else -1
    _face_model.prepare(ctx_id=ctx_id)
    return _face_model
```

**模型路径优先级：**

```python
1. 环境变量 NIU_MODELS_PATH
2. 项目根目录/models（相对于 photo-server 代码位置推导）
```

向量模型（embedding）的路径逻辑相同，代码在 `niu_api/internal/embedding.py`。

### 2.2 向量模型（EmbeddingGemma 2，经 llama.cpp + GGUF）

**唯一支持模型：**

| 配置名 | 来源 | 维度 | max_seq_length | 说明 |
|--------|------|------|----------------|------|
| `embeddinggemma-2`（默认，唯一） | `ggml-org/embeddinggemma-2-GGUF`（源自 `google-deepmind`） | 768 | 8192 | 中文/多语言，GGUF Q8_0 ~310MB，经 llama.cpp 推理 |

> 2026-10-11 起，旧模型 `bge-base-zh-v1.5` / `bge-m3` / `minilm-l12` 已从
> `SUPPORTED_MODELS` 移除，sentence-transformers 依赖链一并摘除。
> 存量 `~/.niu/preferences.json` 里的旧 `embedding_model` 值会被既有守卫
> （`if model_name in SUPPORTED_MODELS`）自动回落，**无需手动改配置**。

**当前默认模型信息：**

| 属性 | 值 |
|------|-----|
| **名称** | embeddinggemma-2 |
| **来源** | `ggml-org/embeddinggemma-2-GGUF`（Google DeepMind 的 EmbeddingGemma 2，GGUF Q8_0 转换） |
| **大小** | ~310MB（另需 `models/llama.cpp/<platform>/llama-server` ~5MB） |
| **维度** | 768 |
| **max_seq_length** | 8192 |
| **许可** | **Apache 2.0**（官方 LICENSE 为标准 Apache 2.0、无附加条款、无 NOTICE 文件） |
| **推理后端** | llama.cpp `llama-server` 子进程，HTTP `127.0.0.1:19878`（避开 uvicorn 9876 与 IM adapter 19877） |

**用途：**
- 文档语义搜索
- 知识检索
- 技能匹配
- MCP 工具描述检索

**前缀（非对称模型的统一口径）：**

EmbeddingGemma 2 官方为非对称用法（query 侧 `task: search result | query: `、
doc 侧 `title: none | text: `），但 LightRAG 1.4.19 的 embedding 调用链上
**没有任何 query/doc 判别信号**（`_priority` 被 `wait_func` 吃掉、contextvar 穿不过
优先级队列 worker）。故 Niu 采用**两侧统一加 `task: search result | query: `**：
PM 实测判别间隔由 +0.1482 提升到 +0.2007（优于官方非对称用法的 +0.1954）。
该前缀在 `niu_api/internal/embedding.py` 的 `EMBEDDING_PREFIX` 单一真相源定义，
由 `_LlamaCppAdapter.encode` 统一施加（全后端唯一注入点）。

**模型配置：**

`~/.niu/preferences.json` 的 `lightrag.embedding_model` 字段。**该字段在 0.5.0 之后
只接受 `embeddinggemma-2`**；写其它值（含旧模型名）会由守卫自动回落，并可按
`config-manager` MCP 工具修改。

**加载逻辑：**

```python
# niu_api/internal/embedding.py（2026-10-11 起）

SUPPORTED_MODELS = {
    "embeddinggemma-2": {
        "local_dir": "llama.cpp",          # 产物路径由 embedding_artifacts 定位
        "hf_id": "ggml-org/embeddinggemma-2-GGUF",
        "dim": 768,
        "backend": "llama-cpp",            # 走 llama-server，不走 sentence-transformers
    },
}

DEFAULT_MODEL = "embeddinggemma-2"

def get_model():
    """加载向量模型（llama.cpp 后端：确保 llama-server 就绪并返回适配器）。"""
    requested_model = _get_embedding_model_name()  # 从 preferences.json 读取，旧值自动回落
    info = SUPPORTED_MODELS[requested_model]
    if info.get("backend") == "llama-cpp":
        llama_server.start()          # 产物缺失/起不来 → 抛异常 → 契约 A
        return _LlamaCppAdapter()
    ...
```

**运行时切换：** 0.5.0 起只有一个模型，`switch_model()` 已无第二个可切目标
（函数保留但无生产调用方）。若将来真的换模型，**不要删整个
`~/.niu/lightrag_storage/`** —— 那会连 3 个真相源（GraphML / full_docs / cache）
一起毁掉。正确做法：**只删 3 个 `vdb_*.json`** 后重启，程序走既有修复链用新模型重建索引
（详见 README「从 0.4.x 升级（必做）」与「已在 0.5.0 上迁移过？」两节）。

### 2.3 Token 计数 tokenizer（DeepSeek-V3）

**文件：** `models/tokenizers/deepseek-v3/tokenizer.json`（7,847,652 B，**必须入 git**）

**用途：** `agent/token_calculator.py` 固定加载它做**本地 token 计数**（上下文百分比、
压缩时机判断都依赖这个数）。DeepSeek-V3 的 BPE 对中文最准确，计数结果与 DeepSeek API 一致。

**为什么单列一节（2026-10-11 教训）：** 它曾因 `.gitignore` 里
「`models/tokenizers/` 被 ignore + `!models/**/*.json` negation 兜着但**从来没人 add**」
这一规则组合而漏出版本库 —— 结果是打包机 Clone 出来的树里没有它，打出的安装包缺文件，
用户端 token 计数静默降级成 litellm 估算，而日志只有一条极易漏看的 WARNING。
**现已在 `.gitignore` 显式注明该目录必须入 git，且文件已跟踪。**

**缺失时的表现：** 不崩溃；`logger.warning("[TokenCalculator] failed to load tokenizer: ..., falling back to litellm")`。
排查见 troubleshooting「上下文百分比显示不准」条。

**新增/更换 tokenizer 后必须自检：** `git ls-files models/tokenizers/` 要有输出。

### 2.4 模型下载

**打包时预下载：**

```bash
# 运行依赖打包脚本
python scripts/package_all_dependencies.py

# 自动下载：
# - Python 依赖包 → python_packages/
# - 人脸识别模型 → models/models/buffalo_l/
# - 向量模型产物 → models/embeddinggemma-2-Q8_0.gguf + models/llama.cpp/<platform>/llama-server
```

> ✅ **已核对（2026-10-11）**：`scripts/package_all_dependencies.py` **已按新口径**下载
> `embeddinggemma-2-Q8_0.gguf` 与 `llama-server`——它以 `niu_api/internal/embedding_artifacts.py`
> 为单一真相源（平台目录、资产名、下载 URL、sha256 校验都从该模块取），
> 并按平台选对 `darwin-x64` / `win32-x64` 的产物。此前挂于此处的「尚需核对」待办已关闭。
> ⛔ **`build.sh` / `pack.bat` 都不会自动跑它**，打包前必须手动执行一次，
> 否则产出没有向量模型的安装包（用户启动即卡契约 A）。
> buffalo_l 禁止程序内下载，用户需手动放到 `models/models/buffalo_l/`（开发环境）或
> `niu.app/Contents/Resources/models/models/buffalo_l/`（打包后），见 README「可选：启用照片处理」。

**手动下载（备选）：**

| 产物 | 下载地址 |
|------|---------|
| **EmbeddingGemma 2 GGUF Q8_0**（309,855,456 字节，须校验大小） | https://huggingface.co/ggml-org/embeddinggemma-2-GGUF |
| **llama.cpp `llama-server`**（按平台：darwin-x64 / win32-x64 等） | https://github.com/ggml-org/llama.cpp/releases |
| **buffalo_l** | https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip |

> 产物落盘后可用 `niu_api/internal/embedding_artifacts.py` 自检：
> `missing_artifacts()` 列出缺哪个、`artifacts_ready()` 判齐备、`verify_gguf(path)` 校验大小。
> **HuggingFace 国内不可达时用镜像** `HF_ENDPOINT=https://hf-mirror.com`；
> GitHub release 直连慢时可用 `https://gh-proxy.com/` 前缀。

**下载镜像（国内用户）：**

如果 GitHub/HuggingFace 下载失败，可设置镜像：
```bash
# HuggingFace 镜像（推荐）
export HF_ENDPOINT=https://hf-mirror.com

# buffalo_l GitHub 镜像备选
BUFFALO_L_MIRRORS = [
    "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
    "https://sourceforge.net/projects/insightface.mirror/files/v0.7/buffalo_l.zip/download",
]
```
