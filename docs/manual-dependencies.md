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
| 2 | `torch==2.2.2` | 项目固定版本，不要升级 |
| 3 | `transformers>=4.41,<5.0` | transformers 5.x 要求 torch>=2.4，与 torch 2.2.2 不兼容 |
| 4 | `huggingface_hub<1` | 1.x 与 sentence-transformers 不兼容 |
| 5 | `lightrag-hku` 必须从 Fork 安装 | `git+https://github.com/abc-lee/LightRAG.git`，不能用 PyPI 版本（缺 PR#2990 修复） |

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
| `sentence-transformers` | ==5.5.1 | 向量模型加载与推理 |
| `numpy` | ==1.26.4 | 数值计算 |

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

### 2.2 向量模型（可配置，默认 bge-base-zh-v1.5）

**支持模型列表：**

| 配置名 | HuggingFace ID | 维度 | max_seq_length | 说明 |
|--------|---------------|------|----------------|------|
| `bge-base-zh-v1.5`（默认） | BAAI/bge-base-zh-v1.5 | 768 | 512 | 中文优化，~391MB |
| `bge-m3` | BAAI/bge-m3 | 1024 | 8192 | 多语言，~2.2GB |
| `minilm-l12` | sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 | 384 | 128 | 多语言（旧默认） |

**当前默认模型信息：**

| 属性 | 值 |
|------|-----|
| **名称** | bge-base-zh-v1.5 |
| **来源** | BAAI/bge-base-zh-v1.5 |
| **大小** | ~391MB |
| **维度** | 768 |
| **max_seq_length** | 512 |
| **语言** | 中文优化（英文也支持） |
| **许可** | MIT |

**用途：**
- 文档语义搜索
- 知识检索
- 技能匹配
- MCP 工具描述检索

**模型配置：**

通过 `~/.niu/preferences.json` 的 `lightrag.embedding_model` 字段配置，值为上表中的配置名（如 `"bge-base-zh-v1.5"`、`"bge-m3"`、`"minilm-l12"`）。

**加载逻辑：**

```python
# niu_api/internal/embedding.py

SUPPORTED_MODELS = {
    "bge-base-zh-v1.5": {
        "local_dir": "bge-base-zh-v1.5",
        "hf_id": "BAAI/bge-base-zh-v1.5",
        "dim": 768,
    },
    "bge-m3": {
        "local_dir": "bge-m3",
        "hf_id": "BAAI/bge-m3",
        "dim": 1024,
    },
    "minilm-l12": {
        "local_dir": "paraphrase-multilingual-MiniLM-L12-v2",
        "hf_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
    },
}

DEFAULT_MODEL = "bge-base-zh-v1.5"

def get_model():
    """加载向量模型（配置驱动，本地优先，GPU 优先）"""
    requested_model = _get_embedding_model_name()  # 从 preferences.json 读取
    models_dir = get_models_dir()
    model_info = SUPPORTED_MODELS[requested_model]
    local_path = models_dir / model_info["local_dir"]

    if local_path.exists():
        model = SentenceTransformer(str(local_path))
    else:
        # 自动下载（需要网络）
        model = SentenceTransformer(model_info["hf_id"])
        model.save(str(local_path))

    # GPU 加速（如果可用）
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)

    return model
```

**运行时切换：**

可通过 `switch_model(new_model)` 函数运行时切换模型，自动更新 `preferences.json` 并在下一次 `get_model()` 调用时加载新模型。若维度不同，需重建 LightRAG 知识库（删除 ~/.niu/lightrag_storage/ 后重新导入）。

### 2.3 模型下载

**打包时预下载：**

```bash
# 运行依赖打包脚本
python scripts/package_all_dependencies.py

# 自动下载：
# - Python 依赖包 → python_packages/
# - 人脸识别模型 → models/models/buffalo_l/
# - 向量模型 → models/bge-base-zh-v1.5/（默认）
```

> ⚠️ `scripts/package_all_dependencies.py` 尚未更新，仍下载旧默认模型（minilm-l12）且 buffalo_l 路径不正确。建议直接手动下载模型，或首次启动时自动下载。

**手动下载（备选）：**

| 模型 | 下载地址 |
|------|---------|
| **buffalo_l** | https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip |
| **bge-base-zh-v1.5** | https://huggingface.co/BAAI/bge-base-zh-v1.5 |
| **bge-m3** | https://huggingface.co/BAAI/bge-m3 |

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

---

## 验证记录

| 序号 | 原文 | 修正后 | 原因 |
|------|------|--------|------|
| 1 | 核心依赖表仅列 6 项（insightface/onnxruntime/sentence-transformers/fastapi/mcp/litellm） | 拆分为"agent 核心"、"API 服务"、"人脸识别"三组，补充 aiosqlite/pydantic/httpx/loguru/watchdog/uvicorn/numpy/opencv-python-headless/Pillow | 各 pyproject.toml 依赖项与原文不一致，缺少多个实际依赖 |
| 2 | 向量模型为 paraphrase-multilingual-MiniLM-L12-v2（384d, 466MB） | 默认模型改为 bge-base-zh-v1.5（768d, ~391MB），增加支持模型列表 | `niu_api/internal/embedding.py` 中 `DEFAULT_MODEL = "bge-base-zh-v1.5"` |
| 3 | 向量模型文件路径 `models/paraphrase-multilingual-MiniLM-L12-v2/` | 当前默认 `models/bge-base-zh-v1.5/`，minilm-l12 保留为可选 | 实际 models 目录内容 |
| 4 | 向量模型许可 Apache 2.0 | bge-base-zh-v1.5 为 MIT 许可 | BAAI 模型采用 MIT 许可 |
| 5 | buffalo_l 文件路径 `models/buffalo_l/` | `models/models/buffalo_l/`（两层 models 目录） | 代码 `local_model_path = models_dir / "models" / "buffalo_l"` 及实际目录结构 |
| 6 | buffalo_l 含 `3d68.onnx` | 实际文件名为 `1k3d68.onnx`（~137MB） | 实际目录内容 |
| 7 | buffalo_l 列出 `README.txt` | 删除，目录中无此文件 | 实际目录内容 |
| 8 | `_detect_available_providers` 用列表推导 `selected = [p for p in priority if p in available]` | 改为循环 + 空列表回退逻辑 | 实际代码实现 |
| 9 | `ctx_id=0 if use_gpu else -1` | `ctx_id = 0 if "CUDAExecutionProvider" in providers else -1`，并补充 DirectML 说明 | 实际代码逻辑，DirectML 场景下 ctx_id 仍为 -1 |
| 10 | 向量模型加载代码示例（固定模型名） | 改为配置驱动（SUPPORTED_MODELS + DEFAULT_MODEL），补充运行时切换 | `embedding.py` 实际实现 |
| 11 | 模型路径优先级 4 级（含 `~/.niu/models`） | 仅 2 级：NIU_MODELS_PATH 环境变量、项目根目录推导 | `get_models_dir()` 实际实现 |
| 12 | 预加载描述 "10秒" | 移除具体秒数，说明只预加载模块代码不加载模型 | `preload_face_model()` 实际实现 |
| 13 | 卸载逻辑无细节 | 补充：后台守护线程每 60 秒检查，不调用 gc.collect() | `_start_model_unload_timer()` 实际实现 |
| 14 | 向量模型下载仅写"自动下载" | 补充 HuggingFace 下载地址和 HF_ENDPOINT 镜像配置 | 实际下载方式 |
| 15 | 性能对比表（具体秒数） | 移除，数值无实测依据 | 文档应避免无实测数据的具体数值 |
