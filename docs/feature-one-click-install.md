# 一键安装 - 详细设计

> 版本：v1.1
> 日期：2026-03-22
> 状态：详细设计完成

---

## 一、设计理念

### 1.1 核心目标

**零配置、双击即用，目标用户是商务白领不是程序员。**

| 传统软件安装 | 本助理安装 |
|--------------|------------|
| 安装 Python 环境 | 内置 Python 运行时 |
| pip install 依赖 | 预装所有依赖 |
| 配置数据库 | 自动初始化 |
| 下载模型 | 首次启动引导下载 |
| 修改配置文件 | 向导式配置 |

### 1.2 必须打包的模型

| 模型 | 用途 | 大小 | 必须性 |
|------|------|------|--------|
| **buffalo_l** | 人脸检测与识别 | 326MB | ✅ 必须 |
| **all-MiniLM-L6-v2** | 文本向量 | 90MB | ✅ 必须 |
| **LLM** | 对话生成 | 用户自行管理 | ❌ 不打包 |

> **说明**：大语言模型（LLM）由用户自行配置，支持：
> - 本地 Ollama
> - 云端 API（OpenAI、Anthropic 等）
> - 其他兼容 API

### 1.3 安装包组成

```
personal-assistant-setup.exe (Windows, ~500MB)
personal-assistant.dmg (macOS, ~500MB)
personal-assistant.AppImage (Linux, ~500MB)

安装后目录结构：
personal-assistant/
├── bin/                      # 可执行文件
│   └── assistant.exe         # 主程序
├── python/                   # 嵌入式 Python 运行时
│   ├── python.exe
│   └── Lib/site-packages/    # 预装依赖
├── tools/                    # Python MCP 工具
│   ├── document-parser/
│   ├── face-recognition/
│   └── vector-store/
├── models/                   # 模型目录（首次启动下载）
│   ├── insightface/          # 人脸识别模型 (326MB)
│   └── embeddings/           # 向量模型 (90MB)
├── data/                     # 用户数据
│   ├── files/
│   ├── photos/
│   └── db/
├── config/                   # 配置文件
└── resources/                # 静态资源
```

---

## 二、Python 运行时嵌入

### 2.1 使用 go-embed-python

基于 [kluctl/go-embed-python](https://github.com/kluctl/go-embed-python) 实现嵌入式 Python：

```go
import (
    "github.com/kluctl/go-embed-python/python"
    "github.com/kluctl/go-embed-python/pip"
)

func main() {
    // 创建嵌入式 Python 实例
    ep, err := python.NewEmbeddedPython("personal-assistant")
    if err != nil {
        panic(err)
    }
    defer ep.Cleanup()
    
    // 获取 Python 可执行文件路径
    exePath, _ := ep.GetExePath()
    fmt.Println("Python path:", exePath)
    
    // 执行 Python 脚本
    cmd, _ := ep.PythonCmd("-c", "print('hello')")
    cmd.Stdout = os.Stdout
    cmd.Run()
}
```

### 2.2 预装 Python 依赖

```go
//go:generate go run generate/generate.go

// generate/generate.go
package main

import (
    "github.com/kluctl/go-embed-python/pip"
)

func main() {
    // 从 requirements.txt 生成嵌入的依赖
    err := pip.CreateEmbeddedPipPackagesForKnownPlatforms(
        "requirements.txt",
        "./internal/python-libs/data/",
    )
    if err != nil {
        panic(err)
    }
}
```

### 2.3 requirements.txt

```
# 文档处理
pypdf>=3.0.0
python-docx>=0.8.11
python-pptx>=0.6.21
openpyxl>=3.1.0
python-magic>=0.4.27
markdown>=3.4.0
beautifulsoup4>=4.12.0
readability-lxml>=0.8.1

# 人脸识别
insightface>=0.7.3
onnxruntime>=1.15.0
opencv-python-headless>=4.8.0
Pillow>=10.0.0

# 向量与搜索
lancedb>=0.3.0
sentence-transformers>=2.2.0

# 图谱
kuzu>=0.4.0

# MCP 协议
mcp>=1.0.0

# vCard 处理
vobject>=0.9.6

# 工具
httpx>=0.25.0
loguru>=0.7.0
pyyaml>=6.0
```

### 2.4 支持的平台

| 平台 | 架构 | Python 版本 |
|------|------|-------------|
| Windows | x86_64 | Python 3.11 |
| macOS | x86_64, arm64 | Python 3.11 |
| Linux | x86_64 | Python 3.11 |

---

## 三、人脸识别模型安装

### 3.1 模型信息

| 属性 | 值 |
|------|-----|
| **名称** | buffalo_l |
| **来源** | InsightFace v0.7 |
| **大小** | 326MB |
| **包含** | SCRFD-10GF（检测）+ ResNet50（识别）+ 2d106/3d68（关键点）+ Gender&Age |
| **许可** | 非商业研究用途 |

### 3.2 模型下载流程

```
首次启动
    │
    ▼
检查 ~/.insightface/models/buffalo_l
    │
    ├── 存在且完整 → 跳过下载
    │
    └── 不存在/不完整 → 显示下载提示
                          │
                          ▼
                    用户确认下载
                          │
                          ▼
                    下载 buffalo_l.zip (326MB)
                    显示进度条
                          │
                          ▼
                    解压到 ~/.insightface/models/buffalo_l/
                          │
                          ▼
                    验证模型完整性
                          │
                          ▼
                    完成
```

### 3.3 下载实现

```python
import os
import zipfile
import httpx
from pathlib import Path
from loguru import logger

INSIGHTFACE_MODELS_DIR = Path.home() / ".insightface" / "models"
BUFFALO_L_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"

# 备用下载源
BUFFALO_L_MIRRORS = [
    "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
    "https://sourceforge.net/projects/insightface.mirror/files/v0.7/buffalo_l.zip/download",
]

class FaceModelDownloader:
    """人脸识别模型下载器"""
    
    REQUIRED_FILES = [
        "det_10g.onnx",      # 人脸检测
        "w600k_r50.onnx",    # 人脸识别
        "2d106det.onnx",     # 2D 关键点
        "genderage.onnx",    # 性别年龄
    ]
    
    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
    
    def is_installed(self) -> bool:
        """检查模型是否已安装"""
        model_dir = INSIGHTFACE_MODELS_DIR / "buffalo_l"
        if not model_dir.exists():
            return False
        
        # 检查必需文件
        for file in self.REQUIRED_FILES:
            if not (model_dir / file).exists():
                return False
        
        return True
    
    async def download(self) -> Path:
        """下载模型"""
        if self.is_installed():
            logger.info("Face model already installed")
            return INSIGHTFACE_MODELS_DIR / "buffalo_l"
        
        model_dir = INSIGHTFACE_MODELS_DIR / "buffalo_l"
        model_dir.mkdir(parents=True, exist_ok=True)
        
        zip_path = INSIGHTFACE_MODELS_DIR / "buffalo_l.zip"
        
        # 尝试多个镜像源
        for url in BUFFALO_L_MIRRORS:
            try:
                await self._download_with_progress(url, zip_path)
                break
            except Exception as e:
                logger.warning(f"Download from {url} failed: {e}")
                continue
        else:
            raise Exception("Failed to download from all mirrors")
        
        # 解压
        logger.info("Extracting model...")
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(model_dir)
        
        # 清理
        zip_path.unlink()
        
        # 验证
        if not self.is_installed():
            raise Exception("Model verification failed")
        
        logger.info("Face model installed successfully")
        return model_dir
    
    async def _download_with_progress(self, url: str, dest: Path):
        """带进度显示的下载"""
        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                
                total = int(response.headers.get("content-length", 0))
                downloaded = 0
                
                with open(dest, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        
                        if self.progress_callback and total:
                            progress = downloaded / total
                            self.progress_callback(progress, downloaded, total)
```

### 3.4 首次启动 UI

```
┌─────────────────────────────────────────────┐
│  安装必要组件                                │
├─────────────────────────────────────────────┤
│                                             │
│  人脸识别模型 (buffalo_l)                   │
│  [████████████████░░░░░░] 65% (212MB/326MB) │
│  正在下载...                                │
│                                             │
│  向量模型 (all-MiniLM-L6-v2)                │
│  [░░░░░░░░░░░░░░░░░░░░░░] 等待中            │
│                                             │
└─────────────────────────────────────────────┘
```

---

## 四、向量模型安装

### 4.1 模型信息

| 属性 | 值 |
|------|-----|
| **名称** | all-MiniLM-L6-v2 |
| **来源** | sentence-transformers |
| **大小** | 90MB |
| **维度** | 384 |
| **语言** | 多语言（支持中英文） |
| **许可** | Apache 2.0 |

### 4.2 可选模型

> **重要说明**：默认安装最小版本 (all-MiniLM-L6-v2)，用户可根据电脑性能自行更换更大的模型。模型越大，语义理解越准确，但需要更多内存和磁盘空间。

| 模型 | 大小 | 内存需求 | 特点 |
|------|------|----------|------|
| **all-MiniLM-L6-v2** | 90MB | 512MB | ✅ 默认安装，多语言支持，适合所有电脑 |
| **bge-small-zh** | 100MB | 512MB | 仅中文，轻量 |
| **bge-large-zh-v1.5** | 1.3GB | 2GB | 中文效果好，需要较好电脑 |
| **bge-m3** | 2.2GB | 4GB | 中文效果最好，需要高性能电脑 |

**推荐配置**：
- 8GB 以下内存：使用 all-MiniLM-L6-v2 或 bge-small-zh
- 8-16GB 内存：可以使用 bge-large-zh-v1.5
- 16GB 以上内存：可以使用 bge-m3

**如何更换模型**：
```yaml
# config/embedding.yaml
model: "BAAI/bge-large-zh-v1.5"  # 修改为你想用的模型
```

首次启动时会自动下载新模型。

### 4.3 下载实现

```python
from sentence_transformers import SentenceTransformer
from pathlib import Path
from loguru import logger

EMBEDDING_MODELS = {
    "all-MiniLM-L6-v2": {
        "name": "sentence-transformers/all-MiniLM-L6-v2",
        "size": "90MB",
        "dimensions": 384,
        "min_memory": "512MB",
        "languages": ["en", "zh", "multilingual"],
        "recommended": True,
        "description": "默认选择，多语言支持，适合所有电脑"
    },
    "bge-small-zh": {
        "name": "BAAI/bge-small-zh",
        "size": "100MB",
        "dimensions": 512,
        "min_memory": "512MB",
        "languages": ["zh"],
        "recommended": False,
        "description": "仅中文，适合所有电脑"
    },
    "bge-large-zh-v1.5": {
        "name": "BAAI/bge-large-zh-v1.5",
        "size": "1.3GB",
        "dimensions": 1024,
        "min_memory": "2GB",
        "languages": ["zh"],
        "recommended": False,
        "description": "中文效果好，需要 8GB+ 内存"
    },
    "bge-m3": {
        "name": "BAAI/bge-m3",
        "size": "2.2GB",
        "dimensions": 1024,
        "min_memory": "4GB",
        "languages": ["zh", "en", "multilingual"],
        "recommended": False,
        "description": "中文效果最好，需要 16GB+ 内存"
    }
}

class EmbeddingModelDownloader:
    """向量模型下载器"""
    
    def __init__(self, progress_callback=None):
        self.progress_callback = progress_callback
    
    def is_installed(self, model_key: str) -> bool:
        """检查模型是否已安装"""
        try:
            model_name = EMBEDDING_MODELS[model_key]["name"]
            # 尝试从缓存加载
            model = SentenceTransformer(model_name)
            return True
        except:
            return False
    
    async def download(self, model_key: str = "all-MiniLM-L6-v2") -> SentenceTransformer:
        """下载模型"""
        model_info = EMBEDDING_MODELS[model_key]
        model_name = model_info["name"]
        
        logger.info(f"Downloading embedding model: {model_name}")
        
        # sentence-transformers 会自动处理下载和缓存
        # 在后台线程中执行
        import asyncio
        loop = asyncio.get_event_loop()
        
        def _load():
            return SentenceTransformer(model_name)
        
        model = await loop.run_in_executor(None, _load)
        
        logger.info(f"Embedding model {model_name} ready")
        return model
```

### 4.4 模型选择 UI

```
┌─────────────────────────────────────────────┐
│  选择向量模型                                │
├─────────────────────────────────────────────┤
│                                             │
│  向量模型用于语义搜索，让助理理解您的意图。  │
│                                             │
│  💡 默认安装最小版本，您可随时更换更大模型。│
│     模型越大效果越好，但需要更好的电脑配置。│
│                                             │
│  ○ all-MiniLM-L6-v2 (推荐)                  │
│    90MB · 多语言支持 · 适合所有电脑         │
│                                             │
│  ○ bge-small-zh                             │
│    100MB · 仅中文 · 适合所有电脑            │
│                                             │
│  ○ bge-large-zh-v1.5                        │
│    1.3GB · 中文效果好 · 需要 8GB+ 内存      │
│                                             │
│  ○ bge-m3                                   │
│    2.2GB · 中文效果最好 · 需要 16GB+ 内存   │
│                                             │
│       [下载并继续]     [稍后下载]           │
└─────────────────────────────────────────────┘
```

---

## 五、首次启动引导

### 5.1 完整引导流程

```
首次启动
    │
    ▼
┌─────────────────────────────────────────────┐
│  欢迎使用个人知识助理                        │
│  "让我们开始设置"                           │
│                                             │
│  本向导将帮助您：                           │
│  • 安装必要的 AI 模型                       │
│  • 配置大语言模型（可选）                   │
│  • 设置数据存储位置                         │
│                                             │
│               [开始设置]                    │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  安装 AI 模型                               │
├─────────────────────────────────────────────┤
│                                             │
│  人脸识别模型 (326MB)                       │
│  [████████████████████] 完成 ✓             │
│                                             │
│  向量模型 (90MB)                            │
│  [████████████░░░░░░░░] 55%                │
│                                             │
│  预计剩余时间：约 30 秒                      │
│                                             │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  配置大语言模型（可选）                      │
├─────────────────────────────────────────────┤
│                                             │
│  助理需要大语言模型才能进行对话。           │
│  您可以选择：                               │
│                                             │
│  ○ 使用本地 Ollama（已检测到 ✓）           │
│    模型：llama3.2, qwen2.5                 │
│                                             │
│  ○ 使用云端 API                            │
│    API Key: [________________]              │
│    提供商: [OpenAI ▼]                       │
│                                             │
│  ○ 稍后配置                                │
│                                             │
│       [继续]     [跳过]                     │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  选择数据存储位置                           │
├─────────────────────────────────────────────┤
│                                             │
│  ○ 默认位置                                │
│    C:\Users\用户名\personal-assistant       │
│                                             │
│  ○ 自定义位置                              │
│    [D:\我的数据\assistant        ] [浏览]   │
│                                             │
│       [继续]                                │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  导入现有数据（可选）                        │
├─────────────────────────────────────────────┤
│                                             │
│  ○ 导入通讯录 (.vcf 文件)                  │
│  ○ 导入照片文件夹                          │
│  ○ 导入文档文件夹                          │
│  ○ 稍后导入                                │
│                                             │
│       [完成设置]                            │
└─────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────┐
│  ✓ 设置完成！                               │
│                                             │
│  已安装：                                   │
│  • 人脸识别模型 (buffalo_l)                 │
│  • 向量模型 (all-MiniLM-L6-v2)              │
│                                             │
│  配置：                                     │
│  • 语言模型：Ollama / 云端 API / 待配置     │
│  • 数据目录：xxx                           │
│                                             │
│               [开始使用]                    │
└─────────────────────────────────────────────┘
```

### 5.2 LLM 配置（用户自行管理）

助理支持多种 LLM 配置方式：

```yaml
# config/llm.yaml

# 方式1：本地 Ollama
provider: ollama
base_url: http://localhost:11434
model: llama3.2

# 方式2：OpenAI API
provider: openai
api_key: sk-xxx
model: gpt-4o

# 方式3：其他兼容 API
provider: custom
base_url: https://api.example.com/v1
api_key: xxx
model: custom-model
```

**首次启动时：**
- 检测本地是否运行 Ollama（检查 localhost:11434）
- 如果有，列出已安装的模型供用户选择
- 如果没有，提供简单的引导链接到 Ollama 官网

---

## 六、安装包大小估算

### 6.1 组件大小

| 组件 | 大小 |
|------|------|
| Go 主程序 | ~50MB |
| Python 运行时 | ~50MB |
| Python 依赖 | ~300MB |
| 人脸识别模型 | 326MB |
| 向量模型 | 90MB |
| **总计** | **~816MB** |

### 6.2 压缩后

使用 7z / NSIS 压缩：

| 平台 | 安装包大小 |
|------|-----------|
| Windows | ~500MB |
| macOS | ~500MB |
| Linux | ~500MB |

---

## 七、多平台打包

### 7.1 Windows 打包

```json
// package.json
{
  "build": {
    "appId": "com.personal-assistant",
    "productName": "个人知识助理",
    "directories": {
      "output": "dist"
    },
    "win": {
      "target": [
        {
          "target": "nsis",
          "arch": ["x64"]
        }
      ],
      "icon": "resources/icon.ico"
    },
    "nsis": {
      "oneClick": false,
      "allowToChangeInstallationDirectory": true,
      "installerIcon": "resources/icon.ico"
    },
    "extraResources": [
      {
        "from": "python",
        "to": "python",
        "filter": ["**/*"]
      },
      {
        "from": "tools",
        "to": "tools",
        "filter": ["**/*"]
      }
    ]
  }
}
```

### 7.2 macOS 打包

```json
{
  "mac": {
    "target": [
      {
        "target": "dmg",
        "arch": ["x64", "arm64"]
      }
    ],
    "icon": "resources/icon.icns",
    "hardenedRuntime": true
  }
}
```

### 7.3 Linux 打包

```json
{
  "linux": {
    "target": [
      {
        "target": "AppImage",
        "arch": ["x64"]
      }
    ],
    "icon": "resources/icons",
    "category": "Office"
  }
}
```

---

## 八、离线安装

### 8.1 完整离线包

提供包含所有模型的离线安装包：

```
personal-assistant-offline.exe (~800MB)
├── 安装程序
├── Python 运行时
├── 所有 Python 依赖
├── 人脸识别模型 (buffalo_l, 326MB)
└── 向量模型 (all-MiniLM-L6-v2, 90MB)
```

### 8.2 在线安装包

轻量安装包，启动时下载模型：

```
personal-assistant-online.exe (~400MB)
├── 安装程序
├── Python 运行时
├── 所有 Python 依赖
└── 模型（首次启动时下载）
```

---

## 九、更新机制

### 9.1 模型更新检查

```python
class ModelUpdater:
    """模型更新器"""
    
    INSIGHTFACE_LATEST_VERSION = "0.7"
    
    async def check_updates(self) -> list[dict]:
        """检查模型更新"""
        updates = []
        
        # 检查人脸识别模型
        model_dir = INSIGHTFACE_MODELS_DIR / "buffalo_l"
        if not model_dir.exists():
            updates.append({
                "name": "buffalo_l",
                "type": "face_recognition",
                "action": "install",
                "size": "326MB"
            })
        
        # 检查向量模型
        # ... 类似逻辑
        
        return updates
```

### 9.2 软件更新

使用 `electron-updater` 自动检查更新：

```typescript
import { autoUpdater } from 'electron-updater';

autoUpdater.checkForUpdatesAndNotify();
```

---

## 十、错误处理

### 10.1 下载失败处理

```python
class DownloadError(Exception):
    """下载失败"""
    pass

async def download_with_retry(
    downloader,
    max_retries: int = 3,
    on_retry: callable = None
):
    """带重试的下载"""
    for attempt in range(max_retries):
        try:
            return await downloader()
        except Exception as e:
            if attempt == max_retries - 1:
                raise DownloadError(f"Download failed after {max_retries} attempts: {e}")
            
            if on_retry:
                on_retry(attempt + 1, e)
            
            await asyncio.sleep(2 ** attempt)  # 指数退避
```

### 10.2 降级方案

```python
class EmbeddingFallback:
    """向量模型降级"""
    
    PRIORITY = ["bge-m3", "all-MiniLM-L6-v2", "bge-small-zh"]
    
    async def get_model(self) -> SentenceTransformer:
        """尝试加载模型，失败时降级"""
        for model_key in self.PRIORITY:
            try:
                model = await self.load_model(model_key)
                logger.info(f"Loaded embedding model: {model_key}")
                return model
            except Exception as e:
                logger.warning(f"Failed to load {model_key}: {e}")
                continue
        
        raise Exception("No embedding model available")
```

---

## 十一、代码量估算

| 组件 | 代码量 |
|------|--------|
| Python 运行时集成 | ~200 行 |
| 人脸模型下载器 | ~200 行 |
| 向量模型下载器 | ~150 行 |
| 首次启动向导 UI | ~400 行 |
| 配置管理 | ~150 行 |
| 更新检查 | ~100 行 |
| 错误处理 | ~100 行 |
| 打包脚本 | ~100 行 |
| **总计** | **~1,400 行** |

---

## 十二、参考资料

### 技术框架

- [go-embed-python](https://github.com/kluctl/go-embed-python) - Go 嵌入 Python 运行时
- [python-build-standalone](https://github.com/astral-sh/python-build-standalone) - 独立 Python 发行版
- [electron-builder](https://www.electron.build/) - Electron 打包

### 模型来源

- [InsightFace Releases](https://github.com/deepinsight/insightface/releases) - 人脸识别模型
- [Sentence Transformers](https://www.sbert.net/) - 向量模型
- [HuggingFace](https://huggingface.co/) - 模型托管

### LLM 配置参考

- [Ollama](https://ollama.com/) - 本地 LLM 运行时
- [OpenAI API](https://platform.openai.com/) - 云端 API

---

*文档结束*
