# Niu 个人知识助理 - 系统说明书

> **版本：** v0.3.0
> **最后更新：** 2026-04-09
> **适用对象：** 主Agent、用户、开发者

---

## 目录

1. [系统概述](#一系统概述)
2. [架构设计](#二架构设计)
3. [向量库系统](#三向量库系统)
4. [依赖管理](#四依赖管理)
5. [模型文件](#五模型文件)
6. [故障排查](#六故障排查)
7. [性能优化](#七性能优化)
8. [用户指南](#八用户指南)
9. [开发者指南](#九开发者指南)
10. [附录](#十附录)

---

## 一、系统概述

### 1.1 产品定位

**Niu 个人知识助理** 是一款面向商务白领的智能知识管理工具，核心特性：

- 🎯 **零配置**：双击即用，无需安装 Python 等环境
- 📦 **全打包**：所有依赖内嵌，无网络要求
- 🖥️ **跨平台**：Windows/macOS/Linux 通用
- 🔒 **本地优先**：数据存储在本地，隐私安全

### 1.2 核心功能

| 功能 | 说明 |
|------|------|
| **文档管理** | 拖入 PDF/Word/PPT/Excel/Markdown 自动入库 |
| **照片管理** | 拖入照片自动入库，AI 人脸识别，搜索人物 |
| **知识搜索** | 语义搜索知识库，理解意图而非关键词 |
| **智能对话** | 多轮对话，上下文理解，主动整理思路 |
| **定时提醒** | 单次/循环提醒，自然语言创建任务 |

### 1.3 技术栈

```
前端：Electron
后端：Go 启动器 + Python API (FastAPI)
AI：GenericAgent + MCP 协议
存储：SQLite (消息、向量、图谱)
模型：InsightFace (人脸) + Sentence Transformers (向量)
```

---

## 二、架构设计

### 2.1 单进程架构

**设计原则：** 所有模块集成到一个进程中，简化部署和打包。

```
┌─────────────────────────────────────────────┐
│         niu_api (单进程，端口 9876)          │
│                                             │
│  ┌───────────────────────────────────────┐ │
│  │         FastAPI 应用                  │ │
│  │                                       │ │
│  │  /chat       ← 主对话接口            │ │
│  │  /scheduler  ← 定时任务管理          │ │
│  │  /inject     ← 知识注入              │ │
│  │                                       │ │
│  │  内部模块：                           │ │
│  │  - Embedding Model (向量模型)        │ │
│  │  - Scheduler (定时任务后台线程)      │ │
│  │  - VectorSearch (向量搜索)           │ │
│  └───────────────────────────────────────┘ │
└─────────────────────────────────────────────┘

MCP 服务器（独立子进程）：
├── photo-server    (照片处理、人脸识别)
├── kg-server       (知识图谱)
├── vector-store    (向量存储)
├── file-parser     (文档解析)
├── memory-server   (记忆提取)
└── scheduler-server(定时任务 MCP 适配)
```

### 2.2 数据流向

```
用户输入 (Electron UI)
    ↓ HTTP POST /chat
niu_api (FastAPI)
    ↓ 动态注入 (向量搜索)
Agent Core (GenericAgent)
    ↓ MCP 协议 (stdio)
MCP 服务器 (photo-server, kg-server...)
    ↓ 返回结果
Agent Core (处理结果)
    ↓ HTTP Response
用户看到回复
```

### 2.3 目录结构

**打包后：**
```
niu-assistant/
├── niu-assistant.exe           # 主程序（Go + 嵌入式 Python）
├── models/                     # 模型文件
│   ├── buffalo_l/              # 人脸识别模型（326MB）
│   │   ├── det_10g.onnx        # 人脸检测
│   │   ├── w600k_r50.onnx      # 人脸识别
│   │   ├── 2d106det.onnx       # 关键点
│   │   └── genderage.onnx      # 性别年龄
│   └── paraphrase-multilingual-MiniLM-L12-v2/  # 向量模型（466MB）
├── config/                     # 配置文件
│   ├── agents/                 # Agent 定义
│   │   ├── niu.md              # 主 Agent
│   │   └── file-processor.md   # 子 Agent
│   ├── mcp-servers.yaml        # MCP 服务器配置
│   ├── user-config.json        # 用户配置（LLM API Key）
│   └── llm-presets.json        # LLM 预设列表
├── data/                       # 用户数据（首次启动创建）
│   ├── files/                  # 文件存储
│   ├── photos/                 # 照片存储
│   ├── vectors.db              # 向量数据库
│   ├── kg.db                   # 知识图谱
│   └── scheduled_tasks.db      # 定时任务
└── docs/                       # 文档
    └── SYSTEM_MANUAL.md        # 本文档
```

**开发环境：**
```
E:\tools\ai-bot\
├── agent/                      # Agent 核心代码
│   ├── generic/               # GenericAgent 实现
│   ├── injector/              # 动态注入
│   └── session_adapter.py     # Session 管理
├── niu_api/                   # FastAPI 服务
│   ├── internal/              # 内部模块（Embedding + Scheduler）
│   └── chat.py                # 主对话接口
├── mcp-servers/               # MCP 服务器
│   ├── photo-server/
│   ├── kg-server/
│   └── ...
├── config/                    # 配置文件
├── models/                    # 模型文件（开发时软链接）
├── memory/                    # 用户记忆
│   └── skills/                # 动态技能
└── docs/                      # 文档
    ├── SYSTEM_MANUAL.md        # 本文档
    ├── spec-L1-summary.md     # L1规范
    └── design-vector-recursive-query.md  # 递归查询设计
```

---

## 三、向量库系统

### 3.1 概述

向量库是系统的**语义大脑**，用于：
- 语义搜索（文档、知识、记忆）
- 工具匹配（MCP工具描述）
- 查询模式匹配（递归检索）
- 技能匹配（Skills）

**向量库路径**：`~/.niu/memory.json` 中的 `workspace.path` + `/vectors.db`

### 3.2 数据结构

#### 数据库表

```sql
CREATE TABLE documents (
    id TEXT PRIMARY KEY,      -- 文档ID
    content TEXT NOT NULL,    -- 内容文本（英文）
    embedding BLOB,           -- 向量（L2归一化）
    metadata TEXT             -- JSON元数据
);
```

#### 向量归一化

**所有入库向量必须做L2归一化**（标准行为）：
```python
import numpy as np

vec = np.array(embedding, dtype=np.float32)
norm = np.linalg.norm(vec)
if norm > 0:
    vec = vec / norm
embedding_blob = vec.tobytes()
```

### 3.3 文档类型

向量库中存储4类文档：

| category | 说明 | 用途 |
|----------|------|------|
| `mcp_tool` | MCP工具描述 | 工具语义匹配 |
| `query_pattern` | 查询模式 | 递归检索 |
| `skill` | 动态技能 | 技能匹配 |
| `document` | 系统文档 | 文档检索 |

### 3.4 Metadata规范

#### 基础字段（所有文档必须有）

```python
{
    "level": "l1",           # 层级标识（小写）
    "category": "...",       # 文档类型
    "language": "en"         # 内容语言（统一英文）
}
```

#### 按类型的扩展字段

**mcp_tool：**
```python
{
    "level": "l1",
    "category": "mcp_tool",
    "language": "en",
    "name": "schedule_task",
    "server": "scheduler-server",
    "description": "Create scheduled tasks...",
    "input_schema": {...}
}
```

**query_pattern：**
```python
{
    "level": "l1",
    "category": "query_pattern",
    "language": "en",
    "type": "query_pattern",
    "is_recursive": True,           # 触发递归查询
    "refined_query": "schedule task", # 第二轮检索关键词
    "description": "Remind user after X minutes"
}
```

**skill：**
```python
{
    "level": "l1",
    "category": "skill",
    "language": "en",
    "name": "photo-processing",
    "description": "...",
    "source": "memory/skills/photo-processing.md",
    "priority": 50,
    "tags": [...],
    "triggers": [...]
}
```

**document：**
```python
{
    "level": "l1",
    "category": "document",
    "language": "en",
    "resource_type": "system_manual",
    "section": "Architecture > Data Flow",
    "title": "Data Flow Architecture"
}
```

### 3.5 递归查询机制

#### 原理

两阶段向量检索，解决用户表达与工具描述语义差异问题：

```
用户输入："remind me in 5 minutes to take medicine"
    ↓ 第一轮检索
查询模式库（query_pattern）
    匹配到："remind me in X minutes"
    提取：refined_query = "schedule task"
    ↓ 第二轮检索
工具描述库（mcp_tool）
    匹配到：schedule_task
```

#### is_recursive标志

`query_pattern`的metadata中包含：
- `is_recursive: True` — 触发递归检索
- `refined_query` — 第二轮检索使用的关键词

#### 安全机制

- 最多递归3次（硬编码上限）
- 防止死循环和数据错误导致的问题

### 3.6 初始化脚本

#### 主脚本

**位置**：`scripts/init_vector_db.py`

**功能**：
1. 创建向量库表结构
2. 同步Skills到向量库
3. 注册MCP工具描述
4. 注册查询模式
5. 注入系统说明书摘要

**执行方式**：
```bash
cd E:/tools/ai-bot
python scripts/init_vector_db.py
```

#### 辅助脚本

| 脚本 | 功能 |
|------|------|
| `scripts/export_all_mcp_tools.py` | 导出所有MCP工具到JSON |
| `scripts/register_all_mcp_tools_from_json.py` | 从JSON批量注册工具到向量库 |
| `scripts/check_mcp_tools_in_db.py` | 检查向量库中的工具状态 |

#### 辅助脚本用法

**导出工具到JSON：**
```bash
python scripts/export_all_mcp_tools.py
# 输出：logs/all_mcp_tools.json
```

**从JSON注册工具（直接操作DB，无需服务运行）：**
```bash
python scripts/register_all_mcp_tools_from_json.py
```

**检查向量库状态：**
```bash
python scripts/check_mcp_tools_in_db.py
# 输出示例：
# MCP tools in vector DB: 73
# By server:
#   config-manager: 20
#   photo-server: 16
#   ...
```

#### 批量注册模式

向量库支持分批注册：
- 一次注册太多可能失败
- 失败时删除成功的，重新注册剩余的
- 直到全部注册完成

这是正常设计，用于处理大规模数据。

### 3.7 规范文档

**L1规范**：`docs/spec-L1-summary.md`
- 统一metadata结构
- L2归一化要求
- 内容格式规范

**递归查询设计**：`docs/design-vector-recursive-query.md`
- 递归检索机制
- 初始查询模式库
- 性能评估

---

## 四、依赖管理

### 3.1 Python 依赖

**核心依赖：**

| 包名 | 版本 | 用途 | 大小 |
|------|------|------|------|
| `insightface` | >=0.7.3 | 人脸识别 | ~10MB |
| `onnxruntime` | >=1.15.0 | ONNX 推理（CPU） | ~10MB |
| `sentence-transformers` | >=2.2.0 | 向量模型 | ~5MB |
| `fastapi` | >=0.115.0 | Web 框架 | ~1MB |
| `mcp` | >=1.0.0 | MCP 协议 | ~1MB |
| `litellm` | >=1.80.0 | LLM 统一接口 | ~2MB |

**可选依赖：**

| 包名 | 用途 | 说明 |
|------|------|------|
| `onnxruntime-gpu` | CUDA GPU 加速 | 需要 NVIDIA GPU + CUDA |
| `onnxruntime-directml` | Windows GPU 加速 | 无需 CUDA，但仅 Windows |

### 3.2 GPU 支持策略

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
    selected = [p for p in priority if p in available]

    return selected if selected else available
```

**用户场景：**

| 用户环境 | 自动选择 | 性能 |
|---------|---------|------|
| 有 NVIDIA GPU + CUDA | CUDA | 🚀 最快（10倍加速） |
| Windows + 任意 GPU | DirectML | ⚡ 快（3倍加速） |
| 无 GPU 或未安装 CUDA | CPU | 🐢 慢但可用 |

**性能对比：**

| 操作 | CPU | DirectML | CUDA |
|------|-----|----------|------|
| 人脸检测（1张照片） | 2-3秒 | 0.8秒 | 0.3秒 |
| 人脸识别（100人） | 5秒 | 1.5秒 | 0.5秒 |
| 批量处理（1000张） | 50分钟 | 15分钟 | 5分钟 |

### 3.3 模型加载时机

**启动时预加载（10秒）：**
```
[PRELOAD] Importing cv2...             ← 1秒
[PRELOAD] Importing InsightFace...     ← 10秒（加载 ONNX Runtime）
[PRELOAD] Pre-load complete
```

**为什么必须预加载？**
- InsightFace 在 MCP stdio 管道中动态导入会**卡死**
- 这是 InsightFace + MCP stdio 的已知问题
- 预加载模块代码（不是模型），避免后续卡死

**模型按需加载（首次使用时）：**
```
用户拖入照片 → get_face_model() → 加载 buffalo_l（326MB） → 人脸识别
```

**模型自动卸载（空闲 5 分钟）：**
```
无人脸识别操作 5分钟 → 自动卸载模型 → 释放 326MB 内存
```

---

## 四、模型文件

### 4.1 人脸识别模型 (buffalo_l)

**模型信息：**

| 属性 | 值 |
|------|-----|
| **名称** | buffalo_l |
| **来源** | InsightFace v0.7 |
| **大小** | 326MB |
| **包含** | SCRFD-10GF（检测）+ ResNet50（识别）+ 关键点 + 性别年龄 |
| **许可** | 非商业研究用途 |

**文件列表：**

```
models/buffalo_l/
├── det_10g.onnx          # 人脸检测（9MB）
├── w600k_r50.onnx        # 人脸识别（166MB）
├── 2d106det.onnx         # 2D 关键点（5MB）
├── 3d68.onnx             # 3D 关键点（可选）
├── genderage.onnx        # 性别年龄（1MB）
└── README.txt            # 模型说明
```

**加载逻辑：**

```python
# mcp-servers/photo-server/src/niu_photo_server/__init__.py

def get_face_model():
    """加载人脸识别模型"""
    models_dir = get_models_dir()  # 优先本地目录
    model = FaceAnalysis(
        name="buffalo_l",
        root=str(models_dir),
        providers=_detect_available_providers()  # 自动检测 GPU/CPU
    )
    model.prepare(ctx_id=0 if use_gpu else -1)
    return model
```

**模型路径优先级：**

```python
1. 环境变量 NIU_MODELS_PATH
2. 程序目录下的 models/
3. 开发环境：项目根目录/models
4. 用户目录：~/.niu/models
```

### 4.2 向量模型 (paraphrase-multilingual-MiniLM-L12-v2)

**模型信息：**

| 属性 | 值 |
|------|-----|
| **名称** | paraphrase-multilingual-MiniLM-L12-v2 |
| **来源** | sentence-transformers |
| **大小** | 466MB |
| **维度** | 384 |
| **语言** | 多语言（中英文效果好） |
| **许可** | Apache 2.0 |

**用途：**
- 文档语义搜索
- 知识检索
- 技能匹配
- MCP 工具描述检索

**加载逻辑：**

```python
# niu_api/internal/embedding.py

def get_model():
    """加载向量模型"""
    models_dir = get_models_dir()
    model_path = models_dir / "paraphrase-multilingual-MiniLM-L12-v2"

    if model_path.exists():
        # 本地模型
        model = SentenceTransformer(str(model_path))
    else:
        # 自动下载（需要网络）
        model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        model.save(str(model_path))

    # GPU 加速（如果可用）
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)

    return model
```

### 4.3 模型下载

**打包时预下载：**

```bash
# 运行依赖打包脚本
python scripts/package_all_dependencies.py

# 自动下载：
# - Python 依赖包 → python_packages/
# - 人脸识别模型 → models/buffalo_l/
# - 向量模型 → models/paraphrase-multilingual-MiniLM-L12-v2/
```

**手动下载（备选）：**

| 模型 | 下载地址 |
|------|---------|
| **buffalo_l** | https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip |
| **向量模型** | 自动下载（sentence-transformers） |

**下载镜像（国内用户）：**

如果 GitHub 下载失败，使用镜像：
```python
# 修改下载脚本中的 URL
BUFFALO_L_MIRRORS = [
    "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
    "https://sourceforge.net/projects/insightface.mirror/files/v0.7/buffalo_l.zip/download",
]
```

---

## 五、故障排查

### 5.1 启动问题

#### 问题：启动时卡在 "Preloading embedding model..."

**可能原因：**
- 正在下载向量模型（466MB，首次启动）
- GPU 驱动问题

**解决方案：**
```bash
# 1. 检查网络
ping huggingface.co

# 2. 手动下载模型
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2').save('models/paraphrase-multilingual-MiniLM-L12-v2')"

# 3. 禁用 GPU（如果驱动有问题）
set CUDA_VISIBLE_DEVICES=-1
niu-assistant.exe
```

#### 问题：启动时卡在 "Importing InsightFace..."（超过 30 秒）

**可能原因：**
- ONNX Runtime 初始化慢
- 多个 ONNX Runtime 版本冲突

**解决方案：**
```bash
# 1. 检查 ONNX Runtime 版本
pip list | grep onnxruntime

# 2. 应该只有一个版本
# 如果有多个，只保留一个：
pip uninstall onnxruntime onnxruntime-directml onnxruntime-gpu
pip install onnxruntime  # CPU 版本（默认）

# 或 GPU 版本（如果有 NVIDIA GPU + CUDA）：
pip install onnxruntime-gpu
```

#### 问题：启动后窗口空白，日志显示 "Main API unavailable"

**可能原因：**
- 端口 9876 被占用
- 防火墙拦截

**解决方案：**
```bash
# 1. 检查端口占用
netstat -ano | findstr :9876

# 2. 更改端口
set NIU_API_PORT=9877
niu-assistant.exe

# 3. 检查防火墙
# Windows Defender → 允许应用通过防火墙 → 添加 niu-assistant.exe
```

### 5.2 人脸识别问题

#### 问题：拖入照片无反应

**可能原因：**
- 模型未加载
- 照片格式不支持
- 内存不足

**诊断步骤：**
```
1. 检查日志：应看到 "[GET_FACE_MODEL] Starting to load InsightFace..."
2. 检查照片：支持 JPG/PNG/WebP/BMP
3. 检查内存：人脸识别需要 ~500MB 内存
```

**解决方案：**
```python
# 1. 手动触发模型加载
# 在对话中输入："识别这张照片的人脸"

# 2. 检查模型文件
ls models/buffalo_l/det_10g.onnx
ls models/buffalo_l/w600k_r50.onnx

# 3. 重新下载模型
python scripts/package_all_dependencies.py
```

#### 问题：人脸识别速度很慢（超过 10 秒/张）

**可能原因：**
- 使用 CPU 模式（无 GPU 或未安装 CUDA）
- 照片分辨率太高
- 检测到多张人脸

**性能优化：**

| 方案 | 效果 | 说明 |
|------|------|------|
| **安装 onnxruntime-gpu** | 🚀 10倍加速 | 需要 NVIDIA GPU + CUDA |
| **安装 onnxruntime-directml** | ⚡ 3倍加速 | Windows 专用，无需 CUDA |
| **降低照片分辨率** | ✅ 2倍加速 | 提前缩小到 1920x1080 |
| **批量处理** | ✅ 1.5倍加速 | 一次拖入多张照片 |

**安装 GPU 版本：**
```bash
# NVIDIA GPU + CUDA
pip uninstall onnxruntime
pip install onnxruntime-gpu

# Windows + 任意 GPU（推荐）
pip uninstall onnxruntime
pip install onnxruntime-directml

# 重启程序
```

#### 问题：人脸识别报错 "insightface not installed"

**可能原因：**
- 依赖未安装
- Python 环境问题

**解决方案：**
```bash
# 检查依赖
pip list | grep insightface

# 安装
pip install insightface>=0.7.3

# 如果是打包版本，重新下载完整安装包
```

### 5.3 定时任务问题

#### 问题：创建提醒后没有收到通知

**可能原因：**
- Scheduler 未启动
- 任务时间已过
- 系统通知被禁用

**诊断步骤：**
```
1. 检查日志：应看到 "[INTERNAL SCHEDULER] Started"
2. 列出任务：在对话中问 "查看所有定时任务"
3. 检查系统通知设置
```

**解决方案：**
```bash
# 1. 检查任务列表
curl http://127.0.0.1:9876/scheduler/tasks

# 2. 手动触发测试
# 创建 1 分钟后的提醒，测试是否收到

# 3. 检查数据库
sqlite3 data/scheduled_tasks.db "SELECT * FROM scheduled_tasks WHERE status='pending';"
```

#### 问题：循环任务（每天提醒）只触发一次

**可能原因：**
- cron 表达式错误
- 任务状态异常

**解决方案：**
```python
# 正确的 cron 表达式示例
"0 8 * * *"      # 每天 8:00
"0 9 * * 1-5"    # 工作日 9:00
"30 12 * * 0"    # 周日 12:30

# 检查任务
# 在对话中问："查看 ID 为 xxx 的任务详情"
```

### 5.4 向量库问题

#### 问题：向量库初始化失败

**可能原因：**
- 向量模型未加载
- 数据库文件损坏
- 磁盘空间不足

**诊断步骤：**
```bash
# 1. 检查向量库文件
ls -la E:/tmp/bot/vectors.db

# 2. 检查向量库状态
python scripts/check_mcp_tools_in_db.py

# 3. 检查磁盘空间
df -h
```

**解决方案：**
```bash
# 1. 删除损坏的向量库
rm E:/tmp/bot/vectors.db

# 2. 重新初始化
python scripts/init_vector_db.py
```

#### 问题：工具注册不完整

**可能原因：**
- 注册过程中断
- 批量注册部分失败

**诊断步骤：**
```bash
# 检查工具数量
python scripts/check_mcp_tools_in_db.py
```

**正常数量参考：**
```
MCP tools in vector DB: 73
By server:
  config-manager: 20
  photo-server: 16
  kg-server: 14
  memory-server: 8
  vector-store: 7
  scheduler-server: 4
  session-manager: 2
  file-parser: 2
```

**解决方案：**
```bash
# 1. 重新注册所有工具
python scripts/export_all_mcp_tools.py
python scripts/register_all_mcp_tools_from_json.py

# 2. 或者重新初始化
rm E:/tmp/bot/vectors.db
python scripts/init_vector_db.py
```

#### 问题：查询模式不匹配

**可能原因：**
- query_pattern未注册
- 用户表达与预设模式差异较大

**说明**：向量检索是语义匹配，multilingual模型支持跨语言检索，不存在语种问题。

**诊断步骤：**
```python
# 检查query_pattern数量
python -c "
import sqlite3
conn = sqlite3.connect('E:/tmp/bot/vectors.db')
cur = conn.execute('SELECT COUNT(*) FROM documents WHERE json_extract(metadata, \"\$.type\") = \"query_pattern\"')
print('Query patterns:', cur.fetchone()[0])
conn.close()
"
```

**正常数量：8个query_pattern**

#### 问题：Skills未同步

**可能原因：**
- Skills文件不存在
- 同步失败

**诊断步骤：**
```bash
# 检查Skills文件
ls memory/skills/

# 检查向量库中的Skills
python -c "
import sqlite3
conn = sqlite3.connect('E:/tmp/bot/vectors.db')
cur = conn.execute('SELECT COUNT(*) FROM documents WHERE json_extract(metadata, \"\$.category\") = \"skill\"')
print('Skills in DB:', cur.fetchone()[0])
conn.close()
"
```

**解决方案：**
```bash
# 重新同步
python scripts/init_vector_db.py
# 或直接操作
python -c "
from agent.injector.sync import get_skill_sync
sync = get_skill_sync(auto_start=False)
sync.scan_and_sync()
"
```

### 5.5 向量搜索问题

#### 问题：搜索结果不准确

**可能原因：**
- 向量模型未正确加载
- 知识库数据量太小
- 搜索词太模糊

**解决方案：**
```python
# 1. 检查模型
# 在对话中问："测试向量搜索：知识管理"

# 2. 增加知识库数据
# 拖入更多文档

# 3. 使用更具体的搜索词
# 差："文档"
# 好："如何管理文档知识库"
```

#### 问题：向量搜索报错 "embedding service error"

**可能原因：**
- 模型未加载
- GPU 内存不足

**解决方案：**
```bash
# 1. 检查模型文件
ls models/paraphrase-multilingual-MiniLM-L12-v2

# 2. 检查 GPU 内存
nvidia-smi

# 3. 使用 CPU 模式（如果 GPU 内存不足）
set CUDA_VISIBLE_DEVICES=-1
niu-assistant.exe
```

### 5.6 数据问题

#### 问题：数据丢失（历史对话、知识库）

**可能原因：**
- 数据库损坏
- 误删除

**数据备份：**
```
重要文件：
- data/messages.db          # 历史对话
- data/vectors.db           # 向量知识库
- data/kg.db                # 知识图谱
- data/scheduled_tasks.db   # 定时任务
- ~/.niu/memory.json        # 用户记忆

备份方式：
定期复制 data/ 目录到安全位置
```

**恢复数据：**
```bash
# 1. 停止程序
# 2. 恢复备份
cp -r backup/data/* data/

# 3. 重启程序
```

#### 问题：数据库文件过大

**解决方案：**
```bash
# 1. 清理旧对话
sqlite3 data/messages.db "DELETE FROM messages WHERE timestamp < datetime('now', '-30 days');"

# 2. 压缩数据库
sqlite3 data/messages.db "VACUUM;"

# 3. 重建向量索引
# 在对话中问："重新生成所有知识库向量"
```

---

## 七、性能优化

### 6.1 内存优化

**内存占用分析：**

| 组件 | 内存占用 | 说明 |
|------|---------|------|
| 基础进程 | ~200MB | Python + FastAPI |
| Embedding 模型 | ~500MB | 常驻内存 |
| 人脸识别模型 | ~326MB | 按需加载，空闲 5 分钟卸载 |
| 向量数据库 | ~100MB | 取决于数据量 |
| MCP 服务器 | ~50MB × 7 | 每个服务器独立进程 |
| **总计** | ~1.5GB | 推荐内存：8GB+ |

**优化策略：**

1. **人脸识别模型自动卸载**
   ```python
   # 已实现：空闲 5 分钟自动卸载
   MODEL_IDLE_TIMEOUT_SECONDS = 300
   ```

2. **向量搜索降级**
   ```python
   # 如果内存不足，禁用 GPU 加速
   device = "cpu"
   ```

3. **减少 MCP 服务器**
   ```yaml
   # config/mcp-servers.yaml
   # 注释掉不常用的服务器
   # file-parser:
   #   enabled: false
   ```

### 6.2 启动速度优化

**启动时间分解：**

| 阶段 | 时间 | 说明 |
|------|------|------|
| Session 初始化 | <1秒 | SQLite 初始化 |
| Embedding 模型加载 | 7秒 | GPU: RTX 4090 |
| Scheduler 启动 | <1秒 | 后台线程 |
| MCP 工具预加载 | 15秒 | 7个 MCP 服务器 |
| **总计** | ~25秒 | 首次启动更慢（下载模型） |

**优化建议：**

1. **延迟加载非关键服务**
   ```python
   # MCP 服务器按需启动（不预加载）
   preload: false
   ```

2. **使用更快的模型**
   ```python
   # 向量模型改用更小的版本
   # all-MiniLM-L6-v2 (90MB) vs paraphrase-multilingual-MiniLM-L12-v2 (466MB)
   ```

3. **SSD 加速**
   ```
   模型加载从 HDD (10秒) → SSD (2秒)
   ```

### 6.3 GPU 加速

**GPU 加速场景：**

| 操作 | CPU | GPU (CUDA) | 加速比 |
|------|-----|-----------|--------|
| 向量编码（1000条） | 10秒 | 1秒 | 10x |
| 人脸检测（100张） | 50秒 | 5秒 | 10x |
| 人脸识别（1000人） | 5秒 | 0.5秒 | 10x |

**启用 GPU 加速：**

```bash
# 1. 安装 CUDA Toolkit 12.x
# 下载地址：https://developer.nvidia.com/cuda-downloads

# 2. 安装 GPU 版本 ONNX Runtime
pip uninstall onnxruntime
pip install onnxruntime-gpu

# 3. 验证
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
# 应输出：['CUDAExecutionProvider', 'CPUExecutionProvider']

# 4. 重启程序
```

**GPU 内存不足：**
```bash
# 降低 batch size
# 或使用 CPU 模式
set CUDA_VISIBLE_DEVICES=-1
```

---

## 八、用户指南

### 8.1 首次启动流程

**第一步：配置 LLM**

首次启动时，如果未配置大模型，系统会自动弹出设置窗口让你输入 API Key。
设置完成后点击"测试连接并保存"，窗口关闭，进入下一步。

**第二步：设置工作目录**

大模型配置成功后，主窗口会打开。
如果是首次使用（memory.json 中存在 `firstRun` 字段），大模型会主动询问你工作目录放在哪里。
直接告诉大模型路径，例如："E:/我的知识库"
大模型会自动帮你完成初始化配置。

**基本操作：**

| 操作 | 方法 |
|------|------|
| **对话** | 直接输入文字 |
| **入库文档** | 拖入 PDF/Word/PPT/Excel/MD 文件 |
| **入库照片** | 拖入 JPG/PNG 照片 |
| **搜索知识** | 问："搜索关于 XXX 的知识" |
| **创建提醒** | 说："明天早上 8 点提醒我开会" |
| **查看任务** | 问："查看所有定时任务" |

### 8.2 LLM 配置

**配置文件**：`config/user-config.json`

```json
{
  "llm": {
    "presetId": "openai",
    "apiKey": "sk-xxx",
    "apiBase": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "type": "openai"
  }
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `presetId` | 预设 ID，对应 llm-presets.json 中的预设 |
| `apiKey` | 你的 API Key |
| `apiBase` | API 端点地址 |
| `model` | 模型名称 |
| `type` | 类型：`openai`（兼容 OpenAI API）或 `anthropic` |

**预设列表**：编辑 `config/llm-presets.json` 查看支持的预设（OpenAI、Anthropic、DeepSeek、Qwen、Ollama 等）。

**修改配置方式**：
- **方式一（推荐）**：告诉大模型"我的 API Key 是 xxx"，大模型用 bash 工具直接写入
- **方式二**：关闭程序后，手动编辑 `config/user-config.json`

### 8.3 知识图谱

```
自动从文档中提取实体和关系，构建知识图谱

查询：
- "XXX 和 YYY 有什么关系？"
- "显示关于 XXX 的知识图谱"
```

### 8.4 记忆管理

```
系统会自动记忆用户信息和偏好

查看记忆：
问："你记得我的什么信息？"

更新记忆：
说："记住我的工作单位是 XXX"

清除记忆：
说："忘记我的工作单位信息"
```

### 8.5 首次使用（firstRun）

**触发条件**：`~/.niu/memory.json` 中存在 `firstRun: true`

**大模型处理流程**：

1. 在 system prompt 中看到"## 首次使用"段落
2. 主动询问用户工作目录
3. 用户回答路径（如：E:/我的知识库）
4. 大模型执行 bash 命令完成设置：

```bash
python -c "
import json
from pathlib import Path
mem = json.load(open(Path.home() / '.niu' / 'memory.json'))
mem['workspace'] = {'path': 'E:/我的知识库', 'createdAt': '2026-04-07'}
del mem['firstRun']
json.dump(mem, open(Path.home() / '.niu' / 'memory.json', 'w'), indent=2)
"
```

5. **初始化向量库**：
   执行 `python scripts/init_vector_db.py`，等待约30秒完成

设置好工作目录后，执行向量库初始化脚本：

```bash
cd <项目根目录>
python scripts/init_vector_db.py
```

等待约 30 秒，向量库初始化完成。此步骤会：
- 创建向量库表结构
- 同步 Skills 到向量库
- 注册 MCP 工具描述
- 注入系统说明书摘要

6. 确认完成：

"工作目录已设置，向量库初始化完成。现在可以开始对话了！"

7. 完成后，下次对话不再出现首次使用提示

**禁止事项**：
- 不要使用 config-manager MCP 工具（已删除）
- 不要询问用户 API Key（由设置窗口处理）
- 只询问工作目录

### 8.6 常见问题

**Q: 数据存储在哪里？**
```
A: 所有数据存储在 data/ 目录，包括：
- 历史对话：data/messages.db
- 知识库：data/vectors.db
- 知识图谱：data/kg.db
- 定时任务：data/scheduled_tasks.db
```

**Q: 可以离线使用吗？**
```
A: 可以！所有功能都支持离线，除了：
- 首次启动下载模型（需要网络）
- 云端 LLM API（需要网络）

本地 Ollama + 预下载模型 = 完全离线使用
```

**Q: 如何备份数据？**
```
A: 定期复制以下目录：
- data/          (用户数据)
- ~/.niu/        (配置和记忆)
```

**Q: 支持多用户吗？**
```
A: 当前版本为单用户设计，所有数据在本地。
多用户支持计划在未来版本中实现。
```

**Q: GPU 加速有什么要求？**
```
A: NVIDIA GPU：
- 显卡：GTX 1060 或更高
- CUDA：安装 CUDA Toolkit 12.x
- 安装：pip install onnxruntime-gpu

Windows + 任意 GPU：
- 安装：pip install onnxruntime-directml
- 无需 CUDA
```

**Q: 如何卸载？**
```
A: 1. 关闭程序
   2. 删除安装目录
   3. 删除用户数据（可选）：
      - C:\Users\用户名\.niu\
```

---

## 九、开发者指南

### 9.1 本地开发

**环境要求：**
```
- Python 3.11+
- Go 1.26+
- Node.js 18+
- SQLite
```

**启动开发环境：**

```bash
# 1. 安装依赖
pip install -r requirements.txt
cd agent && pip install -e .
cd ../mcp-servers/photo-server && pip install -e .
# ... 安装其他 MCP 服务器

# 2. 启动 API
python -m niu_api

# 3. 启动前端（另一个终端）
cd ui/assistant
npm install
npm start

# 或使用 Go 启动器
go run main.go
```

### 8.2 调试技巧

**查看日志：**
```bash
# API 日志
tail -f logs/api_stderr.log

# MCP 服务器日志
tail -f logs/photo-server-preload.log
```

**测试 MCP 工具：**
```bash
# 列出所有 MCP 工具
curl http://127.0.0.1:9876/api/mcp-tools

# 调用 MCP 工具
curl -X POST http://127.0.0.1:9876/api/mcp-call \
  -H "Content-Type: application/json" \
  -d '{"tool": "photo-server/detect_faces", "arguments": {"photo_path": "test.jpg"}}'
```

**数据库调试：**
```bash
# 查看消息历史
sqlite3 data/messages.db "SELECT * FROM messages ORDER BY timestamp DESC LIMIT 10;"

# 查看向量数据
sqlite3 data/vectors.db "SELECT id, content FROM documents LIMIT 10;"

# 查看定时任务
sqlite3 data/scheduled_tasks.db "SELECT * FROM scheduled_tasks;"
```

### 8.3 贡献代码

**代码风格：**
```
Python: ruff format + ruff check
Go: go fmt
```

**提交规范：**
```
feat: 新功能
fix: 修复 bug
docs: 文档更新
refactor: 重构
test: 测试
```

**Pull Request 流程：**
```
1. Fork 仓库
2. 创建分支：git checkout -b feature/xxx
3. 提交代码：git commit -m "feat: xxx"
4. 推送分支：git push origin feature/xxx
5. 创建 Pull Request
```

---

## 十、附录

### 10.1 命令行参数

```bash
niu-assistant.exe [选项]

选项：
  --port=9876       API 端口（默认 9876）
  --settings        打开设置窗口
  --graph           打开知识图谱窗口
  --help            显示帮助
```

### 9.2 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `NIU_API_PORT` | API 端口 | 9876 |
| `NIU_MODELS_PATH` | 模型目录 | 程序目录/models |
| `CUDA_VISIBLE_DEVICES` | GPU 设备 | 所有 GPU |
| `PYTHONUNBUFFERED` | Python 输出缓冲 | 1 |

### 9.3 API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/chat` | POST | 主对话接口 |
| `/chat/sync` | POST | 同步对话（定时任务用） |
| `/session/messages` | GET | 获取历史消息 |
| `/scheduler/tasks` | GET/POST | 定时任务管理 |
| `/api/inject/resources` | POST | 注入知识 |
| `/api/mcp-tools` | GET | 列出 MCP 工具 |
| `/health` | GET | 健康检查 |

### 10.4 许可证

```
Niu 个人知识助理
Copyright (c) 2026

本软件供个人学习和研究使用。
商业使用请联系开发者获取授权。

第三方库许可：
- InsightFace: MIT License (非商业)
- Sentence Transformers: Apache 2.0
- ONNX Runtime: MIT License
- FastAPI: MIT License
```

---

## 十一、更新日志

### v0.3.0 (2026-04-09)

**重大变更：**
- ✅ 新增向量库系统文档（第三章）
- ✅ L1规范统一（spec-L1-summary.md）
- ✅ 递归查询机制文档（design-vector-recursive-query.md）
- ✅ 新增向量库故障排查（5.4节）

**向量库系统：**
- 4类文档：mcp_tool, query_pattern, skill, document
- 统一metadata结构：level, category, language
- L2归一化（标准行为）
- 递归查询机制（is_recursive标志）

**辅助脚本：**
- `export_all_mcp_tools.py` - 导出工具到JSON
- `register_all_mcp_tools_from_json.py` - 从JSON注册
- `check_mcp_tools_in_db.py` - 检查向量库状态

### v0.2.0 (2026-04-06)

**重大变更：**
- ✅ 单进程架构：整合 embedding 和 scheduler 到主进程
- ✅ GPU 自动检测：自动选择 CUDA/DirectML/CPU
- ✅ 依赖打包：所有依赖预下载，无网络要求

**新增功能：**
- 动态技能系统（watchdog 监控）
- 定时任务优化（延迟启动避免时序问题）
- 完整的系统说明书

**修复问题：**
- 移除重复日志
- 修复依赖声明缺失
- 优化启动速度

**已知问题：**
- macOS/Linux 版本未测试
- 多用户支持未实现

---

**文档维护者：** Claude Sonnet 4.6
**技术支持：** 请在 GitHub Issues 提交问题
**官方网站：** https://niu.ai（待建设）

---

*本说明书随程序更新而更新，请确保使用最新版本。*
