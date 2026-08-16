# 故障排查手册

> 本文档从 SYSTEM_MANUAL.md 拆分而来，包含故障排查的详细指引。
> 如需系统概述和架构信息，请参阅 [SYSTEM_MANUAL.md](SYSTEM_MANUAL.md)。

## 一、故障排查

### 1.1 启动问题

#### 当前进程结构

程序启动后包含以下进程（参见 CLAUDE.md）：
- **Rust 启动器**（`./niu` 二进制，Iced splash 启动 + 进程监控）
- **Python API 服务**（`niu_api`，端口 9876，Agent 核心 + MCP 同进程调用）
- **Electron 前端**（精灵窗口 + 聊天窗口，由 Rust 启动器拉起）

启动顺序：Rust 启动器 → Python API → Electron 前端。任一环节失败都会导致启动卡死或窗口空白。

#### 日志路径

| 路径 | 用途 |
|------|------|
| `logs/llm_interaction_YYYYMMDD.log` | 应用层 LLM 交互日志（请求/响应/工具调用） |
| `logs/raw_http/{YYYYMMDD}/` | 两层日志架构：传输层 `NNNNNN.json` + 应用层 `NNNNNN_request.json`/`NNNNNN_response.json` |
| `logs/api_stderr.log` | Python API stderr 输出 |
| `logs/im_adapter_stderr.log` | IM 适配器（飞书等）stderr 输出 |

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
export CUDA_VISIBLE_DEVICES=-1   # macOS/Linux
set CUDA_VISIBLE_DEVICES=-1       # Windows
./niu                             # macOS/Linux（Windows 用 niu.exe）
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
# macOS/Linux
lsof -i :9876
# Windows
netstat -ano | findstr :9876

# 2. 更改端口
# macOS/Linux
export NIU_API_PORT=9877
./niu
# Windows
set NIU_API_PORT=9877
niu.exe

# 3. 检查防火墙
# macOS：系统设置 → 网络 → 防火墙 → 允许 ./niu 入站
# Windows：Windows Defender → 允许应用通过防火墙 → 添加 niu.exe
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

# 2. 检查模型文件（注意是双层 models/models/ 目录）
ls models/models/buffalo_l/det_10g.onnx
ls models/models/buffalo_l/w600k_r50.onnx

# 3. 重新下载模型
python scripts/package_all_dependencies.py
```

**预加载机制说明：**

`preload_face_model()`（`mcp-servers/photo-server/src/niu_photo_server/__init__.py:4163`）在 MCP 启动前调用，**只导入 cv2 和 InsightFace 模块代码，不加载模型本身**。模型按需加载（首次调用 `get_face_model` 时才加载到内存，约 326MB；空闲 5 分钟自动卸载）。如果 "Importing InsightFace..." 卡住超过 30 秒，说明是模块导入阶段的问题，而非模型加载。

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


#### 问题：人脸识别数据排查（误合并 / 数据异常 / 人工拆分）

当主 Agent 需要排查人脸识别底层数据（如误合并人物需拆分、确认某人脸归属、检查向量数据完整性）时，可以直接查询照片数据库。Agent 入库时知道每张照片对应谁，通过数据库可以建立照片与人脸向量的对应关系。

**数据库位置：** `~/.niu/work/photos.db`（SQLite，若 workspace 已自定义则替换为 `{workspace}/photos.db`）

**表结构：**

| 表 | 用途 | 关键字段 |
|----|------|---------|
| `persons` | 人物记录 | `id`（UUID）、`name`（真名，未命名时为 NULL）、`auto_label`（如 `未命名人物_1`）、`center_embedding`（人脸中心向量 BLOB）、`photo_count` |
| `faces` | 人脸向量 | `id`（UUID）、`photo_id`（关联 photos 表）、`person_id`（关联 persons 表）、`embedding`（512 维 float32，2048 字节 BLOB）、`bounding_box`（JSON）、`confidence` |
| `photos` | 照片记录 | `id`（UUID）、`file_path`（完整路径）、`taken_at`（拍摄时间）、`location`、`abstract` |
| `co_occurrences` | 人物共现统计 | `person_a_id`、`person_b_id`、`count` |

**常用查询：**

```bash
# 1. 查看某个人物名下的所有人脸向量（关键：确认误合并）
sqlite3 ~/.niu/work/photos.db "
SELECT f.id, p.file_path, f.confidence, length(f.embedding) as emb_len
FROM faces f
JOIN photos p ON f.photo_id = p.id
WHERE f.person_id = (
  SELECT id FROM persons WHERE name = '人物名'
);"

# 2. 查看所有人物概况（含未命名）
sqlite3 ~/.niu/work/photos.db "
SELECT id, name, auto_label, photo_count
FROM persons ORDER BY photo_count DESC;"

# 3. 查看某个 person 的完整信息（含向量大小）
sqlite3 ~/.niu/work/photos.db "
SELECT id, name, auto_label, photo_count,
       length(center_embedding) as center_emb_len,
       length(name_embedding) as name_emb_len
FROM persons WHERE name = '人物名';"

# 4. 查看所有未命名人物
sqlite3 ~/.niu/work/photos.db "
SELECT id, auto_label, photo_count FROM persons
WHERE name IS NULL OR name = auto_label OR name LIKE '未命名人物%'
ORDER BY auto_label;"

# 5. 查看某张照片对应的人脸记录
sqlite3 ~/.niu/work/photos.db "
SELECT f.id, f.person_id, p.name, p.auto_label, f.confidence
FROM faces f
JOIN persons p ON f.person_id = p.id
WHERE f.photo_id = (
  SELECT id FROM photos WHERE file_path LIKE '%照片文件名%'
);"
```

**误合并拆分流程：**

当用户报告两个人物被错误合并（如"吕英"名下混入了另一个人的照片）：

1. 用查询 1 查出该人物名下所有人脸向量及其对应照片路径
2. Agent 根据入库记忆判断哪张照片属于错误合并的人
3. 将误合并的 face 的 `person_id` 更新到正确人物（或创建新 person）
4. **关键步骤**：重新计算原人物的 `center_embedding`

**为什么必须重算 `center_embedding`：**

`center_embedding` 是人脸匹配的基准向量，采用增量加权平均计算：
`新中心 = (旧中心 × photo_count + 新向量) / (photo_count + 1)`
删除 face 记录时，系统不会自动重算 `center_embedding`。如果不手动重算，旧的双人混合向量还在，后续新照片入库仍会错误匹配到该人物（相似度可能更高，因为混合向量"两头靠"）。

**重算 `center_embedding` 的 Python 脚本：**

```python
import sqlite3
import numpy as np

DB_PATH = "~/.niu/work/photos.db"  # 若 workspace 已自定义则替换
PERSON_ID = "替换为目标人物的 id"

conn = sqlite3.connect(DB_PATH)

# 取剩余所有 face embedding
rows = conn.execute(
    "SELECT embedding FROM faces WHERE person_id = ?", (PERSON_ID,)
).fetchall()

if rows:
    embeddings = [np.frombuffer(r[0], dtype=np.float32) for r in rows]
    new_center = np.mean(embeddings, axis=0)
    conn.execute(
        "UPDATE persons SET center_embedding = ?, photo_count = ? WHERE id = ?",
        (new_center.tobytes(), len(rows), PERSON_ID),
    )
    conn.commit()
    print(f"已重新计算 center_embedding，基于 {len(rows)} 个人脸向量")
else:
    # 没有剩余 face，清空 center
    conn.execute(
        "UPDATE persons SET center_embedding = NULL, photo_count = 0 WHERE id = ?",
        (PERSON_ID,),
    )
    conn.commit()
    print("该人物没有剩余人脸向量，已清空 center_embedding")

conn.close()
```

**注意事项：**
- 向量是 512 维 float32 的 BLOB（2048 字节），`length(embedding)` 应为 2048
- `confidence` 低于 0.6 的人脸向量可能是误检测，需特别关注
- 直接修改数据库后需重启程序使缓存失效
- `auto_label` 编号由 `get_next_auto_label()` 分配，已修复字符串排序 bug（2026-07-30），现在 `_10` 以后会正常递增到 `_11`、`_12`…

### 1.3 定时任务问题

#### 问题：创建提醒后没有收到通知

**通知形态（2026-08-15 起）**：定时提醒到点后——①Chat 页面显示提醒消息（程序写 Message.DB，前端由 DB 变更 SSE 刷新）；②蹦高本地通知；③主 Agent 被唤醒后的话（如"该打开咖啡机了"）推送到 IM（经 should_push_im 闸门——定时提醒置 IM 标志为真）。若 IM 端没收到，是主 Agent 的话没发出（检查 IM 连接与主 Agent 处理），定时提醒本身不直接推 IM。

**可能原因：**
- Scheduler 未启动
- 任务时间已过
- 系统通知被禁用

**诊断步骤：**
```
1. 检查日志：应看到 "[INTERNAL SCHEDULER] Scheduled to start (waiting for system_ready signal)"
   调度器等待 system_ready 信号后启动（最长 60 秒超时回退 + 2 秒安全延迟），
   并非固定延迟启动。若 60 秒内未收到信号会强制启动并打印 warning。
2. 列出任务：在对话中问 "查看所有定时任务"
3. 检查系统通知设置
```

**解决方案：**
```bash
# 1. 检查任务列表
curl http://127.0.0.1:9876/scheduler/tasks

# 2. 手动触发测试
# 创建 1 分钟后的提醒，测试是否收到

# 3. 检查数据库（路径通过 workspace 解析，默认 ~/.niu/work/scheduled_tasks.db）
sqlite3 ~/.niu/work/scheduled_tasks.db "SELECT * FROM scheduled_tasks WHERE status='pending';"
# 若 workspace 已自定义，请替换为 {workspace}/scheduled_tasks.db
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

#### 问题：background_script 任务不触发或不静默

background_script 是后台静默脚本任务（到点执行 Python 脚本，无输出静默、有输出/报错才通知主 Agent）。

**脚本无输出但不静默（收到空通知）/ 有输出却没通知：**
- 日志查 `[BG_SCRIPT]`，应看到"静默完成（无输出）"或"Agent replied"
- 检查脚本 stdout：`print()` 输出=通知，不 print 且退出码 0=静默。用 `print()` 精确控制
- 静默成功返回 `"(silent)"`（非 None），调度器据此走成功路径（one-time 硬删除/recurring reschedule）

**任务被静默删除（没触发就消失）：**
- 脚本文件不存在 → 任务被永久删除（`[BG_SCRIPT] 脚本不存在...永久删除`）。检查 `{workspace}/scripts/{script_file}` 是否存在
- one-time 任务报错 → 永久删除（避免 retry_failed_tasks 无限重置，`[BG_SCRIPT] one-time 任务报错，永久删除`）。修复脚本后需重新创建任务
- recurring 任务报错 → 走失败计数器（连续 3 次标 failed，不再 reschedule）。查 `status` 字段

**脚本执行报错：**
- code_run 合并 stderr 进 stdout，报错文本（含 traceback）随通知发给主 Agent
- 超时 60s：stdout 追加 `[Timeout Error]`，status=error，作为报错通知
- 查日志 `[BG_SCRIPT]` 看执行详情

**脚本 import 同目录模块失败：**
- code_run 把代码写到临时文件执行，`sys.path[0]` 是临时目录而非 cwd。脚本内 `import helper`（helper.py 在 scripts 目录）会 ModuleNotFoundError
- 解决：用 `exec(open('helper.py').read())` 或合并成单文件（cwd 是 scripts 目录，相对路径读写文件正常）

**数据库直查：**
```bash
# background_script 任务的 task_kind/script_file 字段
sqlite3 {workspace}/scheduled_tasks.db \
  "SELECT id, task_kind, script_file, status, content FROM scheduled_tasks WHERE task_kind='background_script';"
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
# 2. 恢复备份到 ~/.niu/（含 workspace 子目录）
cp -r backup/niu/* ~/.niu/

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

**恢复方法：从安装包重新解压模板文件**

`memory.json` 和 `preferences.json` 是模板文件，Rust 启动器首次启动时会从安装包内模板拷贝到 `~/.niu/`（参见 `launcher/src/main.rs` 的 `init_niu_dir`）。若运行中文件损坏或丢失，可从原始安装包重新解压获取模板：

```bash
# macOS/Linux：重新解压安装包到临时目录，取出模板文件
# 假设安装包为 niu.tar.gz
tar -xzf niu.tar.gz -C /tmp/niu-restore
cp /tmp/niu-restore/config/memory.json ~/.niu/
cp /tmp/niu-restore/config/preferences.json ~/.niu/
mkdir -p ~/.niu/skills
# skills 文件需从 memory/skills/ 重新同步（见 1.4 节 Skills 未同步）
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
| 脑区边被意外删除 | 衰减算法配置错误 | 检查 preferences.json 中脑区 priority 是否为新值（permanent/long/medium/short），旧值 core/category 会回退到 medium |
| 知识图谱回答准确度下降/搜索匹配度降低（回答变模糊、答非所问、漏关键信息；搜相关话题搜不到、搜出无关内容）；极端场景才见"查询失败"报错 | vdb 文件内部不一致（matrix/data 行数不匹配、孤儿向量） | **直接重启程序即可自动修复**（启动自检自动重建 matrix，无需删文件）；若重启后仍异常，删 3 个 vdb 文件重启，splash 弹窗后点"尝试修复"触发完整重建（见 1.7.1 简易指引） |

#### 1.7.1 知识图谱损坏修复故障排查

启动时检测到知识图谱损坏（v2：仅 3 真相源 corrupt 或 vdb 与 GraphML 数据不一致），splash 会显示损坏提示 + "尝试修复"按钮。用户点修复后触发 `run_repair_on_user_request`。

**v2 检测逻辑变更**（2026-07-28）：
- 派生 kv_store 文件缺失**不再判为损坏**（脑区/Skills 路径下本来就不写这些文件）
- partial 真相源状态（GraphML 有 + full_docs/cache 缺）**不再判为 unrecoverable**
- 真损坏判定改为**数据一致性检查**：GraphML node/edge 在 vdb 缺对应向量 → major

**v3 检测与自动修复**（2026-08-14）：
- 新增 vdb 文件内部一致性检测（`_check_vdb_internal`）：vdb_entities/vdb_relationships/vdb_chunks 的 matrix 行数 vs data 条数不一致 → major（vdb_matrix_mismatch）——孤儿向量导致查询越界崩溃
- **vdb_matrix_mismatch 启动自动修复，不弹窗**：启动自检检测到后自动从 data.vector 重建 matrix → 重跑检测 → 正常启动
- 用户可观察症状：依赖知识图谱的回答准确度下降（变模糊、答非所问、漏关键信息）；搜索功能匹配度降低（搜相关话题搜不到、搜出无关内容）；显式"查询失败"报错只在极端场景出现——**先重启程序，绝大多数情况自动修复**
- 重启后仍异常（vdb 文件缺失等场景）才走下方"用户简易修复指引"（删 3 个 vdb 文件）

**修复失败的常见症状与排查**：

| 症状 | 可能原因 | 排查方法 |
|------|---------|---------|
| 修复后 3 真相源 sha256 变了 | RegionSync 守护线程没真正停 / 其他守护线程写真相源 | 1. 查日志 "RegionSync 已停止" 是否出现；2. 查日志是否还有 "Sync complete"（说明守护线程没停）；3. 检查 `lightrag_manager.py` finally 块是否还在调 `start_background_sync()`（v9 已删除该调用） |
| 修复报 unrecoverable | 3 真相源之一 corrupt（GraphML XML 解析失败 / full_docs 或 cache JSON 解析失败） | 1. 查 repair_result 里 `_unrecoverable_reason` 字段，看哪个真相源损坏；2. 手工验证对应文件是否能解析；3. 真相源 corrupt 无法自动修复，需从备份恢复 |
| 修复后 vdb 仍缺向量 | repair_vdb_entities / repair_vdb_relationships 失败 | 1. 查 repair_result 里对应函数的 status 字段（ok/error）；2. 查 message 字段看失败原因；3. 重新触发修复 |
| 修复后查询知识图谱报错 | 派生文件格式跟 LightRAG 原生不一致 | 1. 对比重建的派生文件跟 LightRAG 原生格式（字段名/类型）；2. 确认修复走的是 storage.upsert 接口（不是直接写 JSON）；3. 检查 vdb_* 文件的 matrix 是否 L2 归一化 |
| 修复期间程序卡死 | RegionSync stop_background_sync_blocking join 超时 | 1. 查日志是否有 "RegionSync 守护线程在 60s 后仍在运行"；2. 检查 RegionSync _run_sync_impl 是否有死循环；3. 强制 kill 进程后重启 |
| 修复后脑区节点消失 | GraphML 被改写（脑区节点被删） | 1. 对比修复前后 GraphML 的 node 数量；2. 查 RegionSync 是否在修复期间跑了 sync；3. 从备份恢复 GraphML |
| 启动时弹修复窗但用户认为数据正常 | 脑区+Skills 注入后 partial 状态（v2 已修复）| v2 之前 partial 误判为损坏，v2 后合法状态不弹窗。如仍弹窗，检查 vdb 是否真缺向量（`check_all` 输出的 major_errors 应为 0） |

**用户简易修复指引**（推荐主 Agent 告知用户）：

**兜底路径**（v3 自动修复不适用时——vdb 文件缺失、GraphML-vdb 不一致等场景）：删除 3 个 vdb 文件后重启程序，系统会自动触发修复流程重建向量索引：

```bash
# 1. 退出程序
# 2. 删除 3 个 vdb 文件
rm ~/.niu/lightrag_storage/vdb_chunks.json
rm ~/.niu/lightrag_storage/vdb_entities.json
rm ~/.niu/lightrag_storage/vdb_relationships.json
# 3. 重启程序 ./niu，splash 会显示损坏提示，点"尝试修复"
```

原理：vdb 是从 GraphML 派生的向量索引，删除后检测到"GraphML 有 node/edge 但 vdb 缺对应向量"（数据不一致，major），触发修复重建。3 真相源不会被改写。详见 [manual-vector-store.md 9.9 节](manual-vector-store.md#99-用户简易修复指引删-vdb-触发修复)。

**真相源保护验证**（修复前后必须执行）：
```bash
# 修复前记录 sha256
shasum -a 256 ~/.niu/lightrag_storage/graph_chunk_entity_relation.graphml \
            ~/.niu/lightrag_storage/kv_store_full_docs.json \
            ~/.niu/lightrag_storage/kv_store_llm_response_cache.json

# 修复后再次记录，3 个 sha256 必须完全一致
```

如果 sha256 不一致，说明真相源被改写，必须从备份恢复。

**3 真相源 corrupt 的恢复路径**（修复程序无法自动恢复）：
1. GraphML 损坏：从最近备份恢复（如果有），否则只能接受数据丢失，重新入库文档重建图谱
2. full_docs 损坏：从备份恢复，否则文档原文丢失（但 GraphML 还在，实体关系不会丢）
3. cache 损坏：从备份恢复，否则需要重新跑 LLM 抽取（消耗 token + 时间）

**修复程序不会做的事**（用户需了解）：
- 不会自动备份 3 真相源（用户应自己定期备份）
- 不会修复 3 真相源内容（真相源 corrupt 只能从备份恢复）
- 不会重启 RegionSync（修复后必须重启程序让正常启动流程触发）
- 不会增量修复（v9 只做全量重建，删 9 派生全部重建）
- 不会重建空派生文件（脑区/Skills 路径下 full_docs 缺失时，doc_status 等派生走"不写空文件"分支，符合 LightRAG 原生行为）

详细机制见 [manual-vector-store.md 第九章](manual-vector-store.md#九知识图谱损坏检测与自愈修复)。

#### 1.7.2 知识图谱错误分类与修复方法（E3，2026-08-16）

E3 工程后，知识图谱不再静默吞错——查询异常会以错误文本/dict 形式暴露到前端、API 与 LLM 注入段，用户可据此定位并修复。**错误形态 → 修复动作**映射表：

| # | 错误形态（用户可见） | 可能原因 | 修复动作 |
|---|---------------------|---------|---------|
| ① | 前端图谱页显示"知识图谱不可用" | `get_lightrag` 初始化门控拒绝（修复中/损坏/冷却） | 按启动日志结果分支：发现 `[LightRAG] 核心数据损坏（N critical errors），拒绝初始化` / `[LightRAG] 数据不一致（N major errors），拒绝初始化` 等 warning → 按下方 1.7.1 修复流程处理（删 3 个 vdb 文件重启触发修复）；无此类 warning（修复中/冷却态静默返回——冷却为初始化失败后 `_INIT_RETRY_SECONDS=60` 重试窗口）→ 修复中等待修复完成、冷却等待 60s 自动重试（勿删 vdb） |
| ② | API/日志出现"一致性检测失败"（`get_lightrag_status` 返回的 `integrity.check_failed=true` + `integrity.error` 字段） | `run_resilience_phase1` / `get_lightrag_status` 即时检查时 `check_all` 抛异常——检测未完成，不等于数据损坏 | 查 `logs/api_stderr.log` 定位检测异常（如 vdb 文件不可解析）→ 重试或重启程序；若持续 → 按 1.7.1 全量重建 |
| ③ | LLM 注入段出现标注"`[知识检索失败，本轮无参考知识注入]`"（本轮对话没有参考知识注入） | 知识图谱服务不可用（检索 raise → runner except 标注，LLM 感知"检索失败"而非假装无知识） | 检查图谱服务状态（`curl http://127.0.0.1:9876/api/kg/stats`）→ 按门控三态（修复中/损坏/冷却）对应动作：修复中 → 等待修复完成；损坏 → 按 1.7.1 重建；冷却 → 等待 60s 自动重试；恢复后标注自然消失 |
| ④ | adapter 错误文本"知识图谱不可用（初始化门控拒绝）"（对话/工具返回中可见） | `get_lightrag` 返回 None（门控拒绝）——adapter 不再静默返回空结果，改报通用文案 | 按门控三态对应动作（同 ①③）：损坏 → 按 1.7.1 删 vdb 重建；修复中 → 等待修复完成；冷却 → 等待 60s 自动重试 |

**要点**：

- **"检测失败" ≠ "数据损坏"**：`check_failed=true` 只是说明本次一致性检测没跑完（如文件读取异常），不触发修复弹窗、`need_repair=false`——先重试/重启，持续失败才走 1.7.1 重建。
- **"检索失败" ≠ "没有知识"**：注入段出现 `[知识检索失败...]` 标注说明图谱当时不可用，不是知识库为空——修复后重试即可，无需重新导入文档。
- **门控拒绝的三种状态**（详见 1.7.1 与 `lightrag_manager.py` 三级门控）：修复中（repair 执行中，静默返回）→ 等待修复完成；损坏（critical 核心数据损坏 / major 数据不一致，均打印拒绝初始化 warning）→ 走 1.7.1 删 vdb 重建；冷却（初始化失败后 60s 重试窗口，静默返回）→ 等待 60s 自动重试。

### 1.8 Chat 页面消息重复（停止后关闭重开仍重复）

**原因**：停止场景下 `persist_agent_reply` 兜底路径（rv=None）无条件写 full_reply，与 V4 逐条持久化（已写同轮 assistant）重复入库。

**解决**：已修复（2026-08-08）——兜底路径加 persisted_msgs 前缀去重（@ 对齐 + `<tool_use>` 剥除 + 双侧 strip 后前缀比对，命中跳过）。升级后新对话不再重复；历史已重复的消息需手动清理或 /clear。

### 1.9 停止后异步子 Agent 被终止（同步子 Agent 结束后异步也停了）

**原因**：旧版全局停止标志无隔离——单击 /stop 会打断所有正在流式的子 Agent LLM（含异步、程序触发的），子 Agent 退出时还可能清掉主 Agent 的停止意图。

**解决**：已修复（2026-08-08）——停止语义下沉为按子 Agent 来源绑定的谓词：单击只终止同步 user 子 Agent，异步与程序触发子 Agent 不受影响；子 Agent 退出不再清全局停止标志。

---

## 验证记录

| 序号 | 原文 | 修正后 | 原因 |
|------|------|--------|------|
| 1 | 向量模型 466MB，手动下载 paraphrase-multilingual-MiniLM-L12-v2 | 默认模型约 400MB，当前默认 bge-base-zh-v1.5，支持多模型切换 | 默认嵌入模型已从 paraphrase-multilingual-MiniLM-L12-v2 切换为 BAAI/bge-base-zh-v1.5（见 niu_api/internal/embedding.py DEFAULT_MODEL） |
| 2 | 人脸识别需要 ~500MB 内存 | 人脸识别需要约 326MB 内存 | CLAUDE.md 和 photo-server 代码均记录为约 326MB |
| 3 | 检查日志：应看到 "[INTERNAL SCHEDULER] Started"（旧版曾修正为 "delayed 10s"） | 应看到 "[INTERNAL SCHEDULER] Scheduled to start (waiting for system_ready signal)"，调度器等待 system_ready 信号后启动（最长 60 秒超时回退 + 2 秒安全延迟） | service.py:145 + scheduler.py:92-121，start_delayed 实际为等待 _ready_event 信号而非固定延迟 |
| 4 | sqlite3 data/scheduled_tasks.db ...（旧版曾修正为 ~/.niu/scheduled_tasks.db） | sqlite3 {workspace}/scheduled_tasks.db ...（默认 ~/.niu/work/scheduled_tasks.db） | service.py:42-50 优先用 {workspace}/scheduled_tasks.db，~/.niu/scheduled_tasks.db 是旧残留 |
| 5 | 所有 REDACTED_WIN_PATH/vectors.db 硬编码路径 | vectors.db 已废弃，知识检索改用 LightRAG（~/.niu/lightrag_storage/） | vector-store 架构已移除，由 lightrag-server 统一管理知识检索 |
| 6 | ls models/paraphrase-multilingual-MiniLM-L12-v2（向量搜索报错排查） | 默认模型 bge-base-zh-v1.5，向量搜索独立排查已移除（合并到 LightRAG 故障排查） | 默认模型已变更，独立向量搜索概念已不存在 |
| 7 | data/messages.db, data/vectors.db, data/kg.db（数据备份列表） | {workspace}/messages.db, ~/.niu/lightrag_storage/, {workspace}/scheduled_tasks.db 等，并说明路径解析 | vectors.db 和 knowledge.kz* 已废弃，知识检索改用 LightRAG 存储 |
| 8 | sqlite3 data/messages.db "DELETE ... WHERE timestamp ..." | sqlite3 ~/.niu/messages.db "DELETE ... WHERE created_at ..." | messages 表使用 created_at 列（见 agent/session.py），不是 timestamp |
| 9 | 浏览器方法 3 使用 --user-data-dir="%USERPROFILE%\.niu\browser_ext_profile" | 使用 --disable-extensions-except，并说明默认使用用户浏览器配置文件 | launcher.py 不指定 --user-data-dir，使用用户默认 profile 共享 cookies |
| 10 | 人脸识别故障提到 "MCP stdio 通信错误"、"ONNX Runtime stdout 污染" | 说明同进程架构后无 stdio 通信问题，无需检查 JSONRPC 解析 | MCP 已从 stdio 架构迁移到同进程直接调用 |
| 11 | 浏览器故障提到 "Playwright 选择器失效"、检查 "playwright\|browser" 日志 | 改为 WSBridge + Chrome Extension 架构，NiuDomTree 通过 content.js 注入 | browser-server 从 Playwright 迁移到 WSBridge + Extension 架构 |
| 12 | 工具数量 73 个，按旧服务器分类（kg-server:14, vector-store:7, photo-server:16） | 约 70 个，按新服务器分类（lightrag-server:15, photo-server:15, brain-region-server:3, browser-server:3） | kg-server + vector-store 合并为 lightrag-server，各服务器工具数量随版本变化 |
| 13 | MCP 加载故障提到手动启动各 MCP 服务器进程测试 | 改为同进程架构下直接测试模块导入（python -c "from niu_xxx import get_tool_schemas"） | MCP 同进程架构无需启动独立进程 |
| 14 | 1.1 节仅用 Windows 命令（netstat/findstr、niu.exe） | 补充 macOS 命令（lsof -i :9876、./niu），并补充进程结构（Rust 启动器 + Python API + Electron 前端）和日志路径（llm_interaction_YYYYMMDD.log + raw_http 两层架构 + api_stderr.log + im_adapter_stderr.log） | 项目实际部署在 macOS，CLAUDE.md 记录从 Electron 迁移至 Iced/Rust 启动器 |
| 15 | 1.2 节检查 ls models/buffalo_l/det_10g.onnx | 改为 ls models/models/buffalo_l/det_10g.onnx（双层目录） | photo-server __init__.py:972 加载路径为 get_models_dir()/"models"/"buffalo_l"，实际为 models/models/buffalo_l/ |
| 16 | 1.2 节未提预加载机制 | 补充 preload_face_model() 说明（只导入 cv2/InsightFace 模块代码，不加载模型本身） | __init__.py:4163 preload_face_model 注释明确"只导入模块，不加载模型" |
| 17 | 1.5 节恢复命令 cp -r backup/data/* data/ | 改为 cp -r backup/niu/* ~/.niu/ | 项目无 data/ 目录，数据在 ~/.niu/ 和 ~/.niu/work/ |
| 18 | 1.5 节末尾"从项目安装目录的 config/user-data/ 拷贝" | 改为"从安装包重新解压模板文件"（config/user-data/ 目录不存在） | 启动器 init_niu_dir 从安装包内 config/ 模板拷贝 memory.json/preferences.json，无 config/user-data/ 目录 |
