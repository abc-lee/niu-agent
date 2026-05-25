# 故障排查手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，包含故障排查的详细指引。
> 如需系统概述和架构信息，请参阅 [SYSTEM_MANUAL.md](SYSTEM_MANUAL.md)。

## 一、故障排查

### 1.1 启动问题

#### 问题：启动时卡在 "Preloading embedding model..."

**可能原因：**
- 正在下载向量模型（首次启动，默认模型约 400MB）
- GPU 驱动问题

**解决方案：**
```bash
# 1. 检查网络
ping huggingface.co

# 2. 查看当前配置的模型
# 默认模型为 bge-base-zh-v1.5，可在 ~/.niu/preferences.json 的 lightrag.embedding_model 中切换
# 支持的模型：bge-base-zh-v1.5（默认）、bge-m3、minilm-l12

# 3. 手动下载模型（以默认模型为例）
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-base-zh-v1.5').save('models/bge-base-zh-v1.5')"

# 4. 禁用 GPU（如果驱动有问题）
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

### 1.2 人脸识别问题

#### 问题：拖入照片无反应

**可能原因：**
- 模型未加载
- 照片格式不支持
- 内存不足

**诊断步骤：**
```
1. 检查日志：应看到 "[GET_FACE_MODEL] Starting to load InsightFace..."
2. 检查照片：支持 JPG/PNG/WebP/BMP
3. 检查内存：人脸识别需要约 326MB 内存
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

### 1.3 定时任务问题

#### 问题：创建提醒后没有收到通知

**可能原因：**
- Scheduler 未启动
- 任务时间已过
- 系统通知被禁用

**诊断步骤：**
```
1. 检查日志：应看到 "[INTERNAL SCHEDULER] Scheduled to start (delayed 10s)"
   调度器是延迟启动的，启动后约 10 秒才开始检查任务
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
sqlite3 ~/.niu/scheduled_tasks.db "SELECT * FROM scheduled_tasks WHERE status='pending';"
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

### 1.4 LightRAG / 知识检索问题

#### 问题：LightRAG 知识检索无结果

**可能原因：**
- 文档未入库
- 入库处理未完成（异步处理）
- 查询模式不匹配

**诊断步骤：**
```bash
# 1. 检查文档入库状态
# 在对话中让 Agent 调用 lightrag_document_status 工具

# 2. 搜索关键词确认数据存在
# 在对话中让 Agent 调用 lightrag_search_entities 工具

# 3. 检查 LightRAG 存储目录
ls ~/.niu/lightrag_storage/
```

**解决方案：**
```
1. 等待异步处理完成：文档入库是异步操作，大文档可能需要较长时间
2. 尝试不同查询模式：local（局部细节）、global（全局概览）、hybrid（混合）
3. 确认文档格式支持：.doc/.xls/.ppt + WPS 假 .docx 不支持 KG 入库
```

#### 问题：LightRAG 存储损坏

**可能原因：**
- 进程异常退出导致数据写入不完整
- 磁盘空间不足

**诊断步骤：**
```bash
# 检查存储目录文件完整性
ls -la ~/.niu/lightrag_storage/
# 正常应包含：graph_chunk_entity_relation.graphml、kv_store_*.json 等文件
```

**解决方案：**
```bash
# 删除损坏的存储后重启，重新导入文档
rm -rf ~/.niu/lightrag_storage/
# 重启程序后重新导入文档
```

#### 问题：文档入库失败（格式不支持）

**说明：** .doc/.xls/.ppt 及 WPS 生成的假 .docx 不支持 KG 入库。

**诊断步骤：**
```
ingest_document 返回 lightrag: "unsupported" 表示格式不支持
```

**解决方案：**
```
用 Microsoft Office 另存为 .docx/.xlsx/.pptx 后重新入库
```

#### 问题：Skills 未同步

**可能原因：**
- Skills 文件不存在
- 同步失败

**诊断步骤：**
```bash
# 检查 Skills 文件
ls memory/skills/

# 检查 LightRAG 中的 Skills
# 在对话中让 Agent 搜索 Skills 相关内容
```

**解决方案：**
```bash
# 重新同步
python -c "
from agent.injector.sync import get_skill_sync
sync = get_skill_sync(auto_start=False)
sync.scan_and_sync()
"
```

### 1.5 数据问题

#### 问题：数据丢失（历史对话、知识库）

**可能原因：**
- 数据库损坏
- 误删除

**数据备份：**
```
重要文件（路径通过 WORKSPACE_PATH 或 ~/.niu/memory.json 中的 workspace.path 解析）：
- {workspace}/messages.db          # 历史对话
- ~/.niu/lightrag_storage/         # LightRAG 知识检索存储
- {workspace}/scheduled_tasks.db   # 定时任务
- {workspace}/photos.db            # 照片数据库
- ~/.niu/memory.json               # 用户记忆
- ~/.niu/preferences.json          # 用户配置

备份方式：
定期复制 {workspace}/ 目录和 ~/.niu/ 目录到安全位置
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
# 注意：messages 表使用 created_at 列（不是 timestamp）
sqlite3 ~/.niu/messages.db "DELETE FROM messages WHERE created_at < datetime('now', '-30 days');"

# 2. 压缩数据库
sqlite3 ~/.niu/messages.db "VACUUM;"

# 3. 重建知识检索索引
# 删除 LightRAG 存储后重启，重新导入文档
# rm -rf ~/.niu/lightrag_storage/
```

#### 问题：用户数据文件丢失或损坏

**问题：~/.niu/ 下关键文件丢失，导致程序无法正常运行**

**涉及文件：**
- `~/.niu/memory.json` — 用户记忆（身份、偏好、工作目录）
- `~/.niu/preferences.json` — 存储配置
- `~/.niu/skills/` — Skills 技能文件目录

**恢复方法：从项目安装目录的 config/user-data/ 拷贝**

```bash
# Linux/Mac
mkdir -p ~/.niu/skills
cp config/user-data/memory.json ~/.niu/
cp config/user-data/preferences.json ~/.niu/
cp config/user-data/skills/*.md ~/.niu/skills/

# Windows (PowerShell)
mkdir "$env:USERPROFILE\.niu\skills"
copy config\user-data\memory.json "$env:USERPROFILE\.niu\"
copy config\user-data\preferences.json "$env:USERPROFILE\.niu\"
copy config\user-data\skills\*.md "$env:USERPROFILE\.niu\skills\"
```

**注意：** 仅恢复缺失的文件，不要覆盖用户已有的配置。如果 preferences.json 已存在但 memory.json 丢失，只恢复 memory.json。

### 1.6 浏览器自动化插件

#### 插件概述

Niu Browser Assistant 是一个 Chrome Extension，提供结构化网页状态提取和交互操作能力。
安装后，AI 助手可以：自动读取网页内容、点击按钮、填写表单、滚动页面。

插件随软件包分发，位于 `extensions/niu-browser-ext/` 目录。

#### 安装方法

**方法 1：自动安装（推荐）**

如果系统默认浏览器已关闭，AI 助手会自动启动浏览器并加载插件（通过 `--load-extension` 参数）。
无需手动操作。

**方法 2：手动安装（浏览器已打开时）**

1. 打开 Chrome/Edge 浏览器
2. 地址栏输入：`chrome://extensions/`（Chrome）或 `edge://extensions/`（Edge）
3. 开启"开发者模式"（右上角开关）
4. 点击"加载已解压的扩展程序"
5. 选择目录：`[安装目录]/extensions/niu-browser-ext`
6. 插件安装完成，浏览器右上角出现 Niu 图标

**方法 3：权限不足时**

如果无法写入浏览器扩展目录，请用户执行以下操作：

1. 以管理员身份打开命令提示符
2. 运行：`start chrome --load-extension="[安装目录]\extensions\niu-browser-ext" --disable-extensions-except="[安装目录]\extensions\niu-browser-ext"`
3. 或指导用户按方法 2 手动安装

> 注意：browser-server 默认使用用户浏览器配置文件（共享 cookies、登录状态），不指定 --user-data-dir。

#### 验证安装

安装成功后，打开任意网页，按 F12 打开开发者工具，在 Console 中输入：
```javascript
typeof NiuDomTree !== 'undefined'
```
返回 `true` 表示插件工作正常。

#### 故障排查

| 问题 | 原因 | 解决 |
|------|------|------|
| browser_navigate 返回 "Extension not connected" | 插件未安装或浏览器未启动 | 按上述方法安装插件 |
| 页面无交互元素 | 页面是纯图片/Canvas | 使用截图方式判断 |
| 新标签页无法操作 | content_script 未注入 | 刷新页面或等待自动注入 |
| WebSocket 连接失败 | Python 服务未启动 | 重启 AI 助手服务 |

### 1.7 LightRAG / 知识图谱故障

| 症状 | 可能原因 | 排查方法 |
|------|---------|---------|
| 知识图谱查询无结果 | LightRAG 存储未初始化 | 检查 `~/.niu/lightrag_storage/` 目录是否存在且非空 |
| 文档入库后查不到实体 | ainsert 失败 | 查看 API 日志中 `lightrag` 相关错误 |
| 知识图谱响应极慢 | 数据量过大或模型未加载 | 检查 `~/.niu/lightrag_storage/` 大小；确认 embedding 模型已加载 |
| lightrag-server 工具不可用 | 模块未加载 | 检查 `agent/mcp_loader.py` 的 REQUIRED_SERVERS 是否包含 lightrag-server |
| LightRAG 初始化失败 | embedding 模型加载失败 | 检查 `models/bge-base-zh-v1.5/` 目录是否完整；查看日志中 embedding 相关错误；确认 sentence_transformers 已安装 |
| LightRAG 文档处理超时 | 文档过大或 LLM API 响应慢 | 检查 LLM API 连通性；尝试拆分大文档后重新入库；查看日志中 ainsert 超时信息 |
| brain-region-server 工具不可用 | 模块未加载 | 检查 `agent/mcp_loader.py` 的 REQUIRED_SERVERS 是否包含 brain-region-server |
| 脑区同步失败 | region_sync 数据源异常 | 查看 API 日志中 `region_sync` 相关错误；检查 `config/mcp-servers.yaml` 中 brain-region-server 配置 |
| 脑区查询返回 UNKNOWN source_id | 数据源标识缺失 | 检查 region_sync 注入时是否正确设置 source_id 参数 |

---

## 验证记录

| 序号 | 原文 | 修正后 | 原因 |
|------|------|--------|------|
| 1 | 向量模型 466MB，手动下载 paraphrase-multilingual-MiniLM-L12-v2 | 默认模型约 400MB，当前默认 bge-base-zh-v1.5，支持多模型切换 | 默认嵌入模型已从 paraphrase-multilingual-MiniLM-L12-v2 切换为 BAAI/bge-base-zh-v1.5（见 niu_api/internal/embedding.py DEFAULT_MODEL） |
| 2 | 人脸识别需要 ~500MB 内存 | 人脸识别需要约 326MB 内存 | CLAUDE.md 和 photo-server 代码均记录为约 326MB |
| 3 | 检查日志：应看到 "[INTERNAL SCHEDULER] Started" | 应看到 "[INTERNAL SCHEDULER] Scheduled to start (delayed 10s)"，调度器延迟 10 秒启动 | service.py 中 start_scheduler 使用 start_delayed(delay_seconds=10) |
| 4 | sqlite3 data/scheduled_tasks.db ... | sqlite3 ~/.niu/scheduled_tasks.db ... | 数据库路径通过 WORKSPACE_PATH 或 memory.json 解析，默认 ~/.niu/ |
| 5 | 所有 REDACTED_WIN_PATH/vectors.db 硬编码路径 | vectors.db 已废弃，知识检索改用 LightRAG（~/.niu/lightrag_storage/） | vector-store 架构已移除，由 lightrag-server 统一管理知识检索 |
| 6 | ls models/paraphrase-multilingual-MiniLM-L12-v2（向量搜索报错排查） | 默认模型 bge-base-zh-v1.5，向量搜索独立排查已移除（合并到 LightRAG 故障排查） | 默认模型已变更，独立向量搜索概念已不存在 |
| 7 | data/messages.db, data/vectors.db, data/kg.db（数据备份列表） | {workspace}/messages.db, ~/.niu/lightrag_storage/, {workspace}/scheduled_tasks.db 等，并说明路径解析 | vectors.db 和 knowledge.kz* 已废弃，知识检索改用 LightRAG 存储 |
| 8 | sqlite3 data/messages.db "DELETE ... WHERE timestamp ..." | sqlite3 ~/.niu/messages.db "DELETE ... WHERE created_at ..." | messages 表使用 created_at 列（见 agent/session.py），不是 timestamp |
| 9 | 浏览器方法 3 使用 --user-data-dir="%USERPROFILE%\.niu\browser_ext_profile" | 使用 --disable-extensions-except，并说明默认使用用户浏览器配置文件 | launcher.py 不指定 --user-data-dir，使用用户默认 profile 共享 cookies |
| 10 | 人脸识别故障提到 "MCP stdio 通信错误"、"ONNX Runtime stdout 污染" | 说明同进程架构后无 stdio 通信问题，无需检查 JSONRPC 解析 | MCP 已从 stdio 架构迁移到同进程直接调用 |
| 11 | 浏览器故障提到 "Playwright 选择器失效"、检查 "playwright\|browser" 日志 | 改为 WSBridge + Chrome Extension 架构，NiuDomTree 通过 content.js 注入 | browser-server 从 Playwright 迁移到 WSBridge + Extension 架构 |
| 12 | 工具数量 73 个，按旧服务器分类（kg-server:14, vector-store:7, photo-server:16） | 约 70 个，按新服务器分类（lightrag-server:15, photo-server:15, brain-region-server:3, browser-server:3） | kg-server + vector-store 合并为 lightrag-server，各服务器工具数量随版本变化 |
| 13 | MCP 加载故障提到手动启动各 MCP 服务器进程测试 | 改为同进程架构下直接测试模块导入（python -c "from niu_xxx import get_tool_schemas"） | MCP 同进程架构无需启动独立进程 |
