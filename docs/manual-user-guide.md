# 用户操作手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，包含用户指南的详细内容。
> 如需系统概述和架构信息，请参阅 [SYSTEM_MANUAL.md](SYSTEM_MANUAL.md)。

## 一、用户指南

### 1.1 首次启动流程

**初始化用户目录**

Go 启动器首次运行时，会自动执行 `initNiuDir()`：
1. 创建 `~/.niu/` 目录（如果不存在）
2. 将 `memory/` 目录下的模板文件（`memory.json`、`preferences.json`）拷贝到 `~/.niu/`（仅当目标文件不存在时才拷贝，避免覆盖已有配置）

**第一步：配置 LLM**

首次启动时，如果未配置大模型，系统会自动弹出设置窗口让你输入 API Key。
设置完成后点击"测试连接并保存"，窗口关闭，进入下一步。

**第二步：设置工作目录**

大模型配置成功后，主窗口会打开。
如果是首次使用（memory.json 中 `firstRun` 为 `true`），大模型会主动询问你工作目录放在哪里。
`workspace.path` 的默认值为占位文本"请询问用户指定工作目录"，Agent 检测到此占位文本时会主动询问用户设置真实工作目录。
直接告诉大模型路径，例如："E:/我的知识库"
大模型会自动帮你完成初始化配置。

**基本操作：**

| 操作 | 方法 |
|------|------|
| **对话** | 直接输入文字 |
| **入库文档** | 拖入 PDF/Word/PPT/Excel/MD/HTML 文件 |
| **入库照片** | 拖入 JPG/PNG 照片 |
| **搜索知识** | 问："搜索关于 XXX 的知识" |
| **创建提醒** | 说："明天早上 8 点提醒我开会" |
| **查看任务** | 问："查看所有定时任务" |

### 1.2 LLM 配置

**配置文件**：`config/user-config.json`

```json
{
  "llm": {
    "presetId": "openai",
    "apiKey": "sk-xxx",
    "apiBase": "https://api.openai.com/v1/chat/completions",
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
| `apiBase` | API 端点地址（openai 类型含 `/chat/completions` 后缀；anthropic 类型含 `/v1/messages` 后缀） |
| `model` | 模型名称 |
| `type` | 类型：`openai`（兼容 OpenAI API）或 `anthropic` |

**预设列表**：编辑 `config/llm-presets.json` 查看支持的预设。

当前内置预设包括：

| 预设 ID | 名称 | 类型 |
|---------|------|------|
| `openai` | OpenAI GPT-4o Mini | openai |
| `openai-gpt4` | OpenAI GPT-4o | openai |
| `anthropic` | Anthropic Claude 3.5 Sonnet | anthropic |
| `anthropic-haiku` | Anthropic Claude 3.5 Haiku | anthropic |
| `deepseek` | DeepSeek Chat | openai |
| `deepseek-reasoner` | DeepSeek R1 | openai |
| `qwen` | 通义千问 | openai |
| `qianfan` | 百度千帆 | openai |
| `doubao` | 豆包 | openai |
| `moonshot` | Moonshot (Kimi) | openai |
| `glm` | 智谱 GLM-4 Flash | openai |
| `minimax` | MiniMax M2 | openai |
| `minimax-anthropic` | MiniMax M2.7 (Anthropic API) | anthropic |
| `minimax-anthropic-highspeed` | MiniMax M2.7 高速版 (Anthropic API) | anthropic |
| `ollama` | Ollama 本地 | openai |
| `custom` | 自定义 | openai |

**修改配置方式**：
- **方式一（推荐）**：通过设置窗口修改（首次启动自动弹出）
- **方式二**：关闭程序后，手动编辑 `config/user-config.json`

### 1.3 知识图谱

知识图谱基于 LightRAG 引擎，支持文档入库后的自动实体提取和关系构建。

**查询方式**：
- 直接向大模型提问（如："XXX 和 YYY 有什么关系？"）
- 通过知识图谱可视化界面浏览实体关系（`/api/kg/snapshot`、`/api/kg/explore`）

**存储位置**：`~/.niu/lightrag_storage/`（LightRAG 固定存储路径，不随 workspace.path 变化）

**架构说明**：知识图谱和向量检索已统一由 `lightrag-server` 提供（23 个工具），取代了旧版独立的 `vector-store` 和 `kg-server`。旧的 `kg-server`（KuzuDB）和 `vector-store` 已禁用（`preload: false`）。

**注意**：文档入库时，LightRAG 自动完成实体提取、关系构建和向量索引，无需手动操作。

**注意**：并非所有文档格式都支持知识图谱入库，详见下方"1.4 支持的文件格式"。

**入库参数配置**：LightRAG 入库参数（并发数、分片大小、补充提取次数等）可在 `~/.niu/preferences.json` 的 `lightrag` 配置段调整，详见 [知识检索运维手册](manual-vector-store.md) 第 8.5 节。

### 1.4 支持的文件格式

Niu 有两种入库能力，格式支持范围不同：

**文件存储入库**：将文件复制到知识库目录，所有文档格式均支持。

**知识图谱入库**：将文件内容写入 LightRAG 知识图谱，仅支持以下格式。

#### 文件存储支持的格式

| 格式 | 扩展名 | 知识图谱入库 |
|------|--------|-------------|
| PDF | .pdf | 支持 |
| Word | .docx | 支持 |
| Word（旧版） | .doc | **不支持** |
| Excel | .xlsx | 支持 |
| Excel（旧版） | .xls | **不支持** |
| PowerPoint | .pptx | 支持 |
| PowerPoint（旧版） | .ppt | **不支持** |
| 纯文本 | .txt | 支持 |
| Markdown | .md | 支持 |
| CSV | .csv | 支持 |
| JSON | .json | 支持 |
| 日志 | .log | 支持 |
| HTML | .html / .htm | 支持 |

#### 不支持知识图谱入库的格式及原因

| 扩展名 | 原因 |
|--------|------|
| .doc | 旧版二进制格式（OLE2），无法可靠提取纯文本 |
| .xls | 旧版二进制格式（BIFF），无法可靠提取纯文本 |
| .ppt | 旧版二进制格式，无法可靠提取纯文本 |
| WPS 假 .docx | WPS 创建的 .docx 文件实际是 OLE2 格式（旧版 .doc），程序会自动检测并标记为不支持知识图谱入库 |

> **建议**：如果 .doc/.xls/.ppt 文件需要入库知识图谱，请先用 Office 或 WPS 另存为 .docx/.xlsx/.pptx 格式。

#### 照片入库支持的格式

| 格式 | 扩展名 | 说明 |
|------|--------|------|
| JPEG | .jpg / .jpeg | 支持存储 + 人脸识别 |
| PNG | .png | 支持存储 + 人脸识别 |
| GIF | .gif | 支持存储 + 人脸识别 |
| BMP | .bmp | 支持存储 + 人脸识别 |
| WebP | .webp | 支持存储 + 人脸识别 |
| HEIC | .heic / .heif | 支持存储 + 人脸识别 |

> 照片格式仅支持存储和人脸识别，不支持知识图谱入库。

详细格式说明和常见问题，请参阅 [文件格式支持手册](manual-file-formats.md)。

### 1.5 记忆管理

记忆系统分为两层：

**1. 用户长期记忆（memory.json，驻留系统提示词）**
- 存储路径：`~/.niu/memory.json` 的 `permanent` 数组
- 工具：`user_memory_remember`、`user_memory_forget`、`user_memory_list`
- 容量：最多 5 条（1 条工作便签 + 4 条长期记忆），每条不超过 200 token
- 便签（type=task）：新任务自动覆盖旧便签
- 特点：每轮对话自动注入系统提示词，大模型始终可见

**用户信息配置（memory.json 的 user 字段）**：

| 字段 | 说明 | 示例 |
|------|------|------|
| `name` | 用户真实姓名 | 李磊 |
| `nickname` | 用户称呼/昵称，主Agent用此称呼用户 | 老板 |
| `occupation` | 用户职业，影响内容提取和日志编写的专业视角 | 软件工程师 |
| `organization` | 用户工作单位，影响内容提取和日志编写的专业视角 | 某科技公司 |

- 这些信息会自动注入到主Agent和子Agent的系统提示词中
- 缺失时主Agent会主动询问用户并写入
- 修改方式：告诉主Agent"我的职业是XXX"或"我在XXX工作"，主Agent会自动更新 memory.json

**2. 语义记忆（向量库，L0/L1/L2 三层存储）**
- 工具：`remember`、`recall`、`update_memory`、`get_memory_stats`、`cleanup_memories`、`link_memories`
- 特点：基于语义相似度检索，支持大量信息
- 用途：对话摘要、技术笔记、经验知识等

**操作示例**：
- 查看记忆：问 "你记得我的什么信息？"
- 添加记忆：说 "记住我的工作单位是 XXX"
- 删除记忆：说 "忘记我的工作单位信息"

### 1.6 首次使用（firstRun）

**触发条件**：`~/.niu/memory.json` 中 `firstRun` 为 `true`

**大模型处理流程**：

1. 在 system prompt 中看到"## 首次使用"段落
2. 主动询问用户工作目录
3. 用户回答路径（如：E:/我的知识库）
4. 大模型用 bash 工具完成设置：
   - 创建目录（如果不存在）
   - 写入 `~/.niu/memory.json`：设置 `workspace.path`，将 `firstRun` 设为 `false`

> 代码中实际将 `firstRun` 设为 `false`，而非删除该字段。

5. 大模型询问用户基本信息（真实姓名、称呼、职业、工作单位），用户回答后写入 memory.json 的 user 字段

6. 完成后，下次对话不再出现首次使用提示

**禁止事项**：
- 不要询问用户 API Key（由设置窗口处理）
- 只询问工作目录

### 1.7 常见问题

**Q: 数据存储在哪里？**
```
A: 数据分布在两个位置：

~/.niu/ 目录：
- 历史对话：~/.niu/messages.db
- 用户记忆：~/.niu/memory.json
- 知识图谱：~/.niu/lightrag_storage/
- 定时任务：~/.niu/scheduled_tasks.db
- 程序配置：config/ 目录

工作区目录（由 workspace.path 决定）：
- 定时任务：{workspace}/scheduled_tasks.db（优先路径）
- 入库文档：{workspace}/documents/
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
- ~/.niu/        (记忆、对话记录、知识图谱、定时任务、配置)
- {workspace}/documents/   (入库文档)

workspace 路径在 ~/.niu/memory.json 的 workspace.path 字段中。
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

## 验证记录

2026-04-30 验证并修正以下内容：

| 位置 | 原文 | 修正后 |
|------|------|--------|
| 8.2 LLM 预设表 | `minimax` 对应模型名 "MiniMax" | 修正为 "MiniMax M2"（与 llm-presets.json 中 model 字段一致） |
| 8.2 LLM 预设表 | `minimax-anthropic-highspeed` 描述 "MiniMax M2.7 高速版" | 修正为 "MiniMax M2.7 高速版 (Anthropic API)"（与实际 preset description 一致） |
| 8.2 apiBase 说明 | "含 `/chat/completions` 后缀" | 补充说明：openai 类型含 `/chat/completions`，anthropic 类型含 `/v1/messages` |
| 8.3 存储位置 | "工作目录下的 LightRAG 数据文件（由 workspace.path 决定）" | 修正为 `~/.niu/lightrag_storage/`（LightRAG 固定路径，不随 workspace 变化） |
| 8.3 查询方式 | 仅提到"可视化界面浏览" | 补充具体 API 端点：`/api/kg/snapshot`、`/api/kg/explore` |
| 8.3 架构说明 | 无 | 新增：说明 lightrag-server 已统一取代旧版 vector-store 和 kg-server |
| 8.4 语义记忆工具 | 仅列出 `remember`、`recall` | 补充完整工具列表：`remember`、`recall`、`update_memory`、`get_memory_stats`、`cleanup_memories`、`link_memories` |
| 8.4 用户记忆便签 | 无 | 补充：便签（type=task）新任务自动覆盖旧便签 |
| 8.5 首次启动触发 | "memory.json 中存在 `firstRun` 字段" | 修正为 "memory.json 中 `firstRun` 为 `true`"（字段值判断而非字段存在判断） |
| 8.5 首次启动步骤 | 包含手动执行 `init_vector_db.py` 步骤 | 移除：向量库初始化已集成到启动流程，无需手动执行 |
| 8.6 数据存储 | 知识图谱路径 `{workspace}/lightrag/` | 修正为 `~/.niu/lightrag_storage/` |
| 8.6 数据存储 | 定时任务路径仅 `{workspace}/scheduled_tasks.db` | 补充 fallback 路径 `~/.niu/scheduled_tasks.db`，说明优先使用 workspace 路径 |
| 8.6 数据存储 | 历史对话路径 | 确认正确路径为 `~/.niu/messages.db`（单文件 SQLite，非目录） |
| 8.6 数据存储 | 列出 `vectors.db` 和 `{workspace}/lightrag/` | 移除旧向量索引引用，改为 `~/.niu/lightrag_storage/` |
| 8.6 备份 | 备份 `~/.niu/` + `{workspace}/` | 修正为 `~/.niu/` + `{workspace}/documents/`（LightRAG 数据已在 ~/.niu 下） |
