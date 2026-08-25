# 性能优化手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，包含性能优化的详细指引。
> 如需系统概述和架构信息，请参阅 [SYSTEM_MANUAL.md](SYSTEM_MANUAL.md)。

## 一、性能优化

### 1.1 内存优化

**内存占用分析：**

| 组件 | 内存占用 | 说明 |
|------|---------|------|
| 基础进程 | ~200MB | Python + FastAPI |
| Embedding 模型 | ~400MB | 默认 bge-base-zh-v1.5，常驻内存；bge-m3 约 2.2GB |
| 人脸识别模型 | ~326MB | 按需加载，空闲 5 分钟自动卸载 |
| 向量数据库 | ~100MB | 取决于数据量，由 LightRAG 管理 |
| MCP 模块 | ~50MB × 9 | 同进程架构，无独立进程开销 |
| **总计** | ~1.5GB | 使用 bge-base-zh-v1.5；若用 bge-m3 则约 3.3GB，推荐内存：8GB+ |

**优化策略：**

1. **人脸识别模型自动卸载**
   ```python
   # 已实现：空闲 5 分钟自动卸载
   # mcp-servers/photo-server/src/niu_photo_server/__init__.py
   MODEL_IDLE_TIMEOUT_SECONDS = 300
   # 不调用 gc.collect()，让 Python 自然回收（gc.collect 可能导致崩溃）
   # 模型按需加载（首次调用时才加载 ~326MB buffalo_l 模型），不预加载模型本身
   ```

2. **Embedding 模型选择**
   ```python
   # niu_api/internal/embedding.py — 通过 preferences.json 配置
   # 默认: bge-base-zh-v1.5 (~400MB, 768d, 512 tokens, 中文优化)
   # 可选: bge-m3 (~2.2GB, 1024d, 8192 tokens, 多语言)
   #       minilm-l12 (~466MB, 384d, legacy)
   # 内存不足时使用默认 bge-base-zh-v1.5
   ```

3. **减少 MCP 模块加载**
   ```yaml
   # config/mcp-servers.yaml
   # 同进程架构下，9 个 REQUIRED_SERVERS 在启动时均加载（模块导入）
   # preload: true/false 在同进程架构中仅影响是否预加载重量级资源（如模型）
   # 不影响模块注册本身
   ```

**MCP 服务器一览（9 必需 + 1 可选）：**

| 服务器 | 工具数 | preload | 说明 |
|--------|--------|---------|------|
| photo-server | 10 | true | 照片管理 + 人脸识别，按需加载 InsightFace 模型 |
| config-manager | 22 | true | 配置管理（LLM/存储/身份/偏好） |
| memory-server | 3 | true | 智能记忆提取和检索 |
| lightrag-server | 23 | true | 知识图谱 + 向量检索（LightRAG 统一管理） |
| file-parser | 2 | true | 文档解析（PDF/Word/PPT/Excel/MD/HTML） |
| session-manager | 4 | false | 会话管理（消息压缩） |
| scheduler-server | 4 | true | 定时任务（单次/循环提醒） |
| browser-server | 5 | false | 浏览器自动化（Chrome Extension + WSBridge） |
| brain-region-server | 3 | true | 脑区激活控制（手动点亮/熄灭/查询状态） |
| ha-server | 8 | true | Home Assistant 设备控制/场景/自动化/脚本（可选，OPTIONAL_SERVERS） |

### 1.2 启动速度优化

**启动时间分解：**

| 阶段 | 时间 | 说明 |
|------|------|------|
| Session 初始化 | <1秒 | SQLite 初始化 |
| Embedding 模型加载 | ~10秒 GPU / ~30秒 CPU | bge-base-zh-v1.5 (~400MB) |
| Scheduler 启动 | <1秒 | 后台线程 |
| MCP 工具加载 | ~2秒 | 同进程模块导入，9 个必需服务器 |
| **总计** | ~15秒 GPU / ~35秒 CPU | 首次启动需下载模型 |

**优化建议：**

1. **MCP 同进程架构**
   ```python
   # agent/mcp_loader.py — REQUIRED_SERVERS 列表中的 9 个服务器
   # 同进程直接调用，无 stdio 通信开销
   # 首次工具调用延迟：stdio ~4s → 同进程 ~0ms（性能提升 ~40000x）
   ```

2. **延迟加载非关键服务**
   ```yaml
   # config/mcp-servers.yaml — preload: true/false 控制重量级资源的预加载
   # 同进程架构下所有模块均被注册，preload 仅影响模型等大资源的提前加载
   ```

3. **Embedding 模型选择**
   ```json
   // ~/.niu/preferences.json — 通过 lightrag.embedding_model 配置
   {
     "lightrag": {
       "embedding_model": "bge-base-zh-v1.5"  // 默认 ~400MB，中文优化
       // "embedding_model": "bge-m3"          // ~2.2GB，多语言长文本
       // "embedding_model": "minilm-l12"      // ~466MB，legacy
     }
   }
   ```

4. **SSD 加速**
   ```
   模型加载从 HDD (~30秒) → SSD (~10秒)
   ```

### 1.3 GPU 加速

**GPU 加速场景：**

| 场景 | CPU | GPU | 加速比 |
|------|-----|-----|--------|
| 向量编码（1000条） | ~10秒 | ~1秒 | ~10x |
| 人脸检测（100张） | ~50秒 | ~5秒 | ~10x |
| 人脸识别（1000人） | ~5秒 | ~0.5秒 | ~10x |

**GPU 后端自动检测：**

```python
# mcp-servers/photo-server/src/niu_photo_server/__init__.py
# _detect_available_providers() 自动检测可用的 ONNX Runtime ExecutionProvider
# 优先级：CUDAExecutionProvider > DmlExecutionProvider > CPUExecutionProvider
```

**Embedding 模型 GPU：**

```python
# niu_api/internal/embedding.py
# get_device() 自动检测 GPU（通过 torch.cuda.is_available()）
# 有 GPU 时使用 "cuda"，否则降级到 "cpu"
```

**启用 GPU 加速：**

> 默认 `requirements.txt` 安装的是 CPU 版 `onnxruntime`（`onnxruntime==1.23.2`），GPU 版本（onnxruntime-gpu / onnxruntime-directml）在 requirements.txt 中被注释掉。启用 GPU 需先卸载 CPU 版再安装对应 GPU 版。

1. **NVIDIA GPU（CUDA）**
   ```bash
   # 卸载 CPU 版后安装 GPU 版本 ONNX Runtime
   pip uninstall onnxruntime
   pip install onnxruntime-gpu

   # 验证
   python -c "import onnxruntime as ort; print(ort.get_available_providers())"
   # 应包含：CUDAExecutionProvider
   ```

2. **Windows AMD/Intel GPU（DirectML）**
   ```bash
   # 安装 DirectML 版本
   pip install onnxruntime-directml

   # 验证
   python -c "import onnxruntime as ort; print(ort.get_available_providers())"
   # 应包含：DmlExecutionProvider
   ```

3. **GPU 内存不足**
   ```bash
   # 禁用 GPU（强制使用 CPU）
   set CUDA_VISIBLE_DEVICES=-1    # Windows
   export CUDA_VISIBLE_DEVICES=-1  # Linux/macOS
   ```

### 1.4 并发与序列化

**单 Worker 模式：**

```python
# niu_api/__main__.py
# uvicorn.run(...) 未显式传 workers 参数（默认单 worker），避免多进程竞态
```

**聊天请求序列化：**

```python
# niu_api/compat.py
# _chat_lock = asyncio.Lock()  — 同一时间只处理一个聊天请求
# 防止并发请求导致上下文混乱
```

**Embedding 模型线程安全：**

```python
# niu_api/internal/embedding.py
# _model_lock = threading.Lock()  — 保护模型加载/切换的线程安全
```

### 1.5 常见性能问题

**问题 1：首次启动慢（需下载模型）**

**原因**：首次启动需要下载 Embedding 模型（bge-base-zh-v1.5 ~400MB）

**解决**：
```bash
# 预下载模型到 models/bge-base-zh-v1.5/ 目录
python scripts/download_model.py
```

**问题 2：人脸识别后内存不释放**

**原因**：InsightFace 模型占用 ~326MB，卸载时需避免 gc.collect() 导致崩溃

**解决**：代码已修复 — 空闲 5 分钟自动卸载，不调用 `gc.collect()`，让 Python 自然回收
```python
# mcp-servers/photo-server/src/niu_photo_server/__init__.py
# MODEL_IDLE_TIMEOUT_SECONDS = 300  — 5 分钟无使用自动卸载
# unload_face_model()  — 手动卸载（设 _face_model = None，不调 gc.collect()）
```

**问题 3：MCP 工具调用延迟**

**原因**：旧架构使用 stdio 通信，每次调用需启动子进程 + JSON-RPC 序列化

**解决**：已升级到同进程架构（MCP In-Process），直接 Python 函数调用
```python
# agent/mcp_loader.py — 9 个 REQUIRED_SERVERS 同进程加载
# agent/tool_registry.py — 全局工具注册中心
# 首次工具调用延迟：stdio ~4s → 同进程 ~0ms（性能提升 ~40000x）
```

### 1.6 LightRAG 性能特征

LightRAG 是工具数最多的 MCP 服务器（23 个工具），承担知识图谱查询、文档索引、实体管理等核心功能，其性能对整体体验影响最大。

**查询延迟：**

| 查询模式 | 延迟 | 说明 |
|----------|------|------|
| bypass + keywords | ~0ms | 跳过 LLM，直接向量检索，近乎即时 |
| naive | ~1-3秒 | 纯向量检索，无图谱推理 |
| local / global | ~3-10秒 | 单次 LLM 调用 + 图谱检索 |
| hybrid / mix | ~5-15秒 | 多次 LLM 调用 + 图谱推理，最全面 |
| only_need_context=True | 减少 50%+ | 跳过 LLM 生成，仅返回检索上下文 |

```python
# niu_api/internal/lightrag_adapter.py — LightRAGAdapter.query()
# timeout: int = 120  — 默认查询超时 120 秒
# keywords 参数：提供后跳过 LLM 关键词提取，直接检索（near-instant）
# only_need_context=True：跳过 LLM 生成步骤，仅返回检索结果
```

**索引构建开销：**

| 操作 | 延迟 | 说明 |
|------|------|------|
| 文档插入（小文档 <1KB） | ~5-15秒 | 分块 + LLM 实体/关系抽取 + 写入图谱 |
| 文档插入（大文档 >10KB） | ~30-120秒 | 分块数增加，LLM 调用次数线性增长 |
| 实体插入 | ~3-10秒 | 单次 LLM 调用 + 向量编码 |
| 删除实体 | ~5-10秒 | 图谱遍历 + 清理关联关系 |
| 文档状态查询 | ~1秒 | 直接查询处理队列 |

```python
# 索引构建为异步后台任务，不阻塞主线程
# pipeline_enqueue_file 通过 LightRAG 内部队列异步处理
# 删除操作 timeout=300 秒（图谱遍历耗时较长）
```

**内存占用：**

- LightRAG 运行时依赖 Embedding 模型（已计入 1.1 内存表），不额外占用模型内存
- 图谱数据存储在磁盘（Neo4j/NetworkX + 向量库），内存占用取决于查询缓存
- `_adapter` 和 `_ingester` 为单例，线程安全（`_adapter_lock` / `_ingester_lock`）

## 二、本地自建大模型部署建议

> 本节说明系统针对**本地自建大模型**场景的设计动机与部署要求（2026-08-24 MD 中继重构工程后定稿）。

### 设计动机

本地部署大模型普遍面临三个约束：**预加载速度慢**（模型载入耗时远高于云端 API）、**处理速度不足**（推理吞吐有限）、**上下文窗口不足**（常见 8K-32K）。系统的整理管道架构针对这些约束做了专门优化：

- **MD 文件中继管道**（2026-08-24 重构）：对话内容镜像到本地文件（F1 提炼源 → F2 梦境队列 → F3 梦境工作集），提炼与梦境进化两个子 Agent 改为**自读文件**而非程序注入大段历史——大幅削减单次请求的提示词体积，适配小上下文窗口
- **F3 工作集按 64KB 软预算切分**、按完整对话单元对齐，梦境进化分多轮消化积压——单次请求负载有界，不会因积压撑爆上下文
- **空内容零调用**：无待处理内容时直接跳过子 Agent 派发，不浪费本地模型的加载与推理时间

### 并发纪律：单线程访问大模型

本地模型通常难以承受并发请求。系统在编排层做了系统性规避：

- **全局整理队列**：所有整理类管道（睡眠/手动压缩/内部溢出）全局一次只跑一个，单 worker 串行消费
- **管道内部严格串行**：睡眠顺序为 journal-agent → context-manager → entity-extractor → dream-evolver（多轮循环），任何时刻只有一个子 Agent 在访问大模型
- **聊天互斥**：`_chat_lock` 保证同一时间只处理一个聊天请求
- **知识图谱单写者**：LightRAG 所有写路径收敛到单一事件循环线程，向量库无并发写

综上，除用户主动并发发起多个会话外，系统在绝大多数情况下对大模型是**单线程串行访问**。

### 部署要求

| 项目 | 要求 | 原因 |
|---|---|---|
| 上下文窗口 | **≥128K** | F3 工作集（64KB 软预算）+ 系统提示词 + 工具 schema + 思考链输出需要足够余量；窗口过小会在长对话或梦境精加工时触发上下文溢出保护 |
| 思考链 | **在模型启动参数上关闭** | 很多模型不支持通过请求参数（命令）关闭思考链——思考链会占用大量输出 token 与推理时间；若模型侧无法关闭，建议换用支持关闭的模型或在启动参数中禁用 |

### 验证记录

以下列出本次验证中修正的文档内容（原文 vs 修正后）：

以下列出本次验证中修正的文档内容（原文 vs 修正后）：

| # | 位置 | 原文 | 修正后 | 原因 |
|---|------|------|--------|------|
| 1 | 6.1 内存表 | Embedding 模型 ~500MB 常驻内存 | Embedding 模型 ~400MB，默认 bge-base-zh-v1.5 | 默认模型已从 all-MiniLM-L6-v2 (90MB) 更换为 bge-base-zh-v1.5 (~400MB)，代码中 DEFAULT_MODEL = "bge-base-zh-v1.5" |
| 2 | 6.1 内存表 | MCP 服务器 ~50MB × 7 每个服务器独立进程 | MCP 模块 ~50MB × 9 同进程架构 | mcp_loader.py REQUIRED_SERVERS 有 9 个服务器；架构已从 stdio 进程通信升级为同进程模块导入 |
| 3 | 6.1 内存表 | 总计 ~1.5GB | 总计 ~1.5GB（bge-base-zh-v1.5）；若用 bge-m3 则约 3.3GB | 200MB 基础 + 400MB embedding + 326MB 人脸 + 100MB 向量库 + 50MB×9 模块 ≈ 1476MB；bge-m3 模式：200+2200+326+100+450 ≈ 3276MB |
| 4 | 6.1 优化策略 | 向量搜索降级 device = "cpu" | Embedding 模型选择（bge-base-zh-v1.5/bge-m3/minilm-l12） | 代码不使用手动 device="cpu" 配置，而是通过 get_device() 自动检测；Embedding 模型已改为可配置 |
| 5 | 6.1 优化策略 | 减少 MCP 服务器 enabled: false | 减少 MCP 模块加载 preload: false | config/mcp-servers.yaml 不使用 enabled 字段，而是 preload: true/false；且 REQUIRED_SERVERS 中 9 个服务器必须加载 |
| 6 | 6.2 启动时间 | Embedding 模型加载 7秒 GPU: RTX 4090 | ~10秒 GPU / ~30秒 CPU bge-base-zh-v1.5 | 模型从 90MB 换为 ~400MB，加载时间相应变化 |
| 7 | 6.2 启动时间 | MCP 工具预加载 15秒 7个服务器 | MCP 工具加载 ~2秒 9个必需服务器 | 同进程架构模块导入远快于 stdio 子进程启动 |
| 8 | 6.2 启动时间 | 总计 ~25秒 | ~15秒 GPU / ~35秒 CPU | 基于新的 Embedding 模型和同进程 MCP 架构重新估算 |
| 9 | 6.2 优化建议 | 使用更快的模型 all-MiniLM-L6-v2 vs MiniLM-L12-v2 | Embedding 模型选择 bge-base-zh-v1.5/bge-m3/minilm-l12 | all-MiniLM-L6-v2 已不在 SUPPORTED_MODELS 中；配置方式改为 preferences.json |
| 10 | 6.2 优化建议 | 延迟加载非关键服务 preload: false | MCP 同进程架构 + 延迟加载 | 同进程架构是主要性能提升，延迟加载是辅助手段 |
| 11 | 6.3 GPU | 仅 CUDAExecutionProvider | 支持 CUDA + DirectML + CPU 三种后端自动检测 | 代码 _detect_available_providers() 优先级 CUDA > DML > CPU |
| 12 | 6.3 GPU | 安装 onnxruntime-gpu + CUDA Toolkit | 分别列出 NVIDIA (onnxruntime-gpu) 和 AMD/Intel (onnxruntime-directml) | Windows 用户可能使用 DirectML 后端，代码已支持 |
| 13 | 6.3 GPU | GPU 内存不足 set CUDA_VISIBLE_DEVICES=-1 (仅 Windows) | 增加 Linux/macOS 的 export 命令 | 跨平台兼容性 |
| 14 | 新增 | 无 6.4 并发与序列化 | 添加 6.4 并发与序列化 小节 | 代码中有 _chat_lock、_model_lock 等并发控制机制，文档未提及 |
| 15 | 新增 | 无 6.5 常见性能问题 | 添加 6.5 常见性能问题 小节 | 原文档缺少故障排除内容；常见问题需与当前代码架构一致 |
| 16 | 6.1 优化策略 | 人脸识别模型"空闲 5 分钟卸载"无额外说明 | 补充：模型按需加载，首次调用时才加载 ~326MB | preload_face_model() 只预导入 cv2/insightface 模块代码，不预加载模型本身；模型通过 get_face_model() 按需加载 |
| 17 | 6.1/6.2 | preload: true/false 描述为"不预加载" | 补充：同进程架构下所有模块均注册，preload 仅影响大资源预加载 | mcp_loader.py 无条件加载所有 REQUIRED_SERVERS，preload 字段在新架构中仅影响重量级资源（如模型/浏览器实例） |
