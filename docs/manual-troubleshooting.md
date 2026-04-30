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

### 1.4 向量库问题

#### 问题：向量库初始化失败

**可能原因：**
- 向量模型未加载
- 数据库文件损坏
- 磁盘空间不足

**诊断步骤：**
```bash
# 1. 检查向量库文件
# 路径通过 WORKSPACE_PATH 或 ~/.niu/memory.json 中的 workspace.path 解析
# 默认: {workspace.path}/vectors.db
ls -la ~/.niu/vectors.db

# 2. 检查向量库状态
python scripts/check_mcp_tools_in_db.py

# 3. 检查磁盘空间
df -h
```

**解决方案：**
```bash
# 1. 删除损坏的向量库
rm ~/.niu/vectors.db

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

**正常数量参考（随版本迭代可能变化）：**
```
MCP tools in vector DB: 约 68
By server:
  config-manager: 20
  photo-server: 9
  lightrag-server: 15
  memory-server: 9
  browser-server: 5
  scheduler-server: 4
  session-manager: 4
  file-parser: 2
```

**解决方案：**
```bash
# 1. 重新注册所有工具
python scripts/export_all_mcp_tools.py
python scripts/register_all_mcp_tools_from_json.py

# 2. 或者重新初始化
rm ~/.niu/vectors.db
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
conn = sqlite3.connect('~/.niu/vectors.db')
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
conn = sqlite3.connect('~/.niu/vectors.db')
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

### 1.5 向量搜索问题

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
# 默认模型: bge-base-zh-v1.5
ls models/bge-base-zh-v1.5/
# 可选模型: paraphrase-multilingual-MiniLM-L12-v2
ls models/paraphrase-multilingual-MiniLM-L12-v2/

# 2. 检查 GPU 内存
nvidia-smi

# 3. 使用 CPU 模式（如果 GPU 内存不足）
set CUDA_VISIBLE_DEVICES=-1
niu-assistant.exe
```

### 1.6 数据问题

#### 问题：数据丢失（历史对话、知识库）

**可能原因：**
- 数据库损坏
- 误删除

**数据备份：**
```
重要文件（路径通过 WORKSPACE_PATH 或 ~/.niu/memory.json 中的 workspace.path 解析）：
- {workspace}/messages.db          # 历史对话
- {workspace}/vectors.db           # 向量知识库
- {workspace}/knowledge.kz*        # 知识图谱（Kuzu 数据库）
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

# 3. 重建向量索引
# 在对话中问："重新生成所有知识库向量"
```

### 1.7 浏览器自动化插件

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

### 1.8 LightRAG / 知识图谱故障

| 症状 | 可能原因 | 排查方法 |
|------|---------|---------|
| 知识图谱查询无结果 | LightRAG 存储未初始化 | 检查 `~/.niu/lightrag_storage/` 目录是否存在且非空 |
| 文档入库后查不到实体 | ainsert 失败 | 查看 API 日志中 `lightrag` 相关错误 |
| 知识图谱响应极慢 | 数据量过大或模型未加载 | 检查 `~/.niu/lightrag_storage/` 大小；确认 embedding 模型已加载 |
| lightrag-server 工具不可用 | 模块未加载 | 检查 `agent/mcp_loader.py` 的 REQUIRED_SERVERS 是否包含 lightrag-server |

---

## 验证记录

| 序号 | 原文 | 修正后 | 原因 |
|------|------|--------|------|
| 1 | 向量模型 466MB，手动下载 paraphrase-multilingual-MiniLM-L12-v2 | 默认模型约 400MB，当前默认 bge-base-zh-v1.5，支持多模型切换 | 默认嵌入模型已从 paraphrase-multilingual-MiniLM-L12-v2 切换为 BAAI/bge-base-zh-v1.5（见 niu_api/internal/embedding.py DEFAULT_MODEL） |
| 2 | 人脸识别需要 ~500MB 内存 | 人脸识别需要约 326MB 内存 | CLAUDE.md 和 photo-server 代码均记录为约 326MB |
| 3 | 检查日志：应看到 "[INTERNAL SCHEDULER] Started" | 应看到 "[INTERNAL SCHEDULER] Scheduled to start (delayed 10s)"，调度器延迟 10 秒启动 | service.py 中 start_scheduler 使用 start_delayed(delay_seconds=10) |
| 4 | sqlite3 data/scheduled_tasks.db ... | sqlite3 ~/.niu/scheduled_tasks.db ... | 数据库路径通过 WORKSPACE_PATH 或 memory.json 解析，默认 ~/.niu/ |
| 5 | 所有 REDACTED_WIN_PATH/vectors.db 硬编码路径 | ~/.niu/vectors.db（并说明路径解析机制） | vector-store 和 vector_search.py 通过环境变量/memory.json 动态解析路径 |
| 6 | ls models/paraphrase-multilingual-MiniLM-L12-v2（向量搜索报错排查） | 同时列出 models/bge-base-zh-v1.5/（默认）和 models/paraphrase-multilingual-MiniLM-L12-v2/（可选） | 默认模型已变更 |
| 7 | data/messages.db, data/vectors.db, data/kg.db（数据备份列表） | {workspace}/messages.db, {workspace}/vectors.db, {workspace}/knowledge.kz*，并说明路径解析 | 知识图谱使用 Kuzu 数据库（knowledge.kz*），非 kg.db；消息库在 ~/.niu/ 下 |
| 8 | sqlite3 data/messages.db "DELETE ... WHERE timestamp ..." | sqlite3 ~/.niu/messages.db "DELETE ... WHERE created_at ..." | messages 表使用 created_at 列（见 agent/session.py），不是 timestamp |
| 9 | 浏览器方法 3 使用 --user-data-dir="%USERPROFILE%\.niu\browser_ext_profile" | 使用 --disable-extensions-except，并说明默认使用用户浏览器配置文件 | launcher.py 不指定 --user-data-dir，使用用户默认 profile 共享 cookies |
| 10 | 人脸识别故障提到 "MCP stdio 通信错误"、"ONNX Runtime stdout 污染" | 说明同进程架构后无 stdio 通信问题，无需检查 JSONRPC 解析 | MCP 已从 stdio 架构迁移到同进程直接调用 |
| 11 | 浏览器故障提到 "Playwright 选择器失效"、检查 "playwright\|browser" 日志 | 改为 WSBridge + Chrome Extension 架构，NiuDomTree 通过 content.js 注入 | browser-server 从 Playwright 迁移到 WSBridge + Extension 架构 |
| 12 | 工具数量 73 个，按旧服务器分类（kg-server:14, vector-store:7, photo-server:16） | 约 68 个，按新服务器分类（lightrag-server:15, photo-server:9） | kg-server + vector-store 合并为 lightrag-server，各服务器工具数量随版本变化 |
| 13 | MCP 加载故障提到手动启动各 MCP 服务器进程测试 | 改为同进程架构下直接测试模块导入（python -c "from niu_xxx import get_tool_schemas"） | MCP 同进程架构无需启动独立进程 |
