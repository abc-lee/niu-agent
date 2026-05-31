# 文档/照片入库功能恢复设计

## 背景

单个照片入库（ingest_photo）和单个文档入库（ingest_document）流程完整可用。
目录入库存在三个问题需要修复。

## 需要修复的三个问题

### 问题1：目录入库不走完整流程

**现状**：
- `ingest_photos_batch()` 只复制文件，不做EXIF提取、人脸识别、写DB、同步KG
- 文档目录入库不存在，收到目录时直接报错 `DIRECTORY_NO_PHOTOS`

**修复**：目录入库 = 子Agent多轮调用 ingest 工具，每次处理一个文件。
类似 `ls | more`——不是重新执行命令，是命令记住进度，继续往下翻页。

### 问题2：ingest_photo 不支持 move/reference 模式

**现状**：`ingest_photo()` 忽略 mode 参数，始终 copy。

**修复**：三种模式的行为：
- `copy`：复制文件到目标路径（现有行为）
- `move`：移动文件到目标路径
- `reference`：不移动文件，只在KG中建立实体，记录原始路径

需要在 `ingest_photo()` 中实现 move 和 reference 逻辑。文档入库同理。

### 问题3：EXIF信息未写入KG实体

**现状**：`extract_exif()` 提取了EXIF数据，存入了DB，但没有传入 `sync_photo_to_kg()`。
KG实体的 description 只有 `照片文件: {filename}`，丢失了拍摄时间、相机、GPS等信息。

**修复**：在 `sync_photo_to_kg()` 中将EXIF信息格式化写入 entity description。

## 核心流程

### 跟 ls | more 一样的工具循环

```
子Agent调用 ingest(dir_path, mode="copy")
  → 发现3个图片和2个文档，先处理第1个文件（图片）
  → 返回 success + 进度信息 + 下一个是文档需要分类

子Agent调用 ingest(dir_path, mode="copy", category="", _offset=1)
  → 第2个文件是文档，没有category
  → 返回 need_category + 文档预览 + 可选分类

子Agent判断分类，调用 ingest(dir_path, mode="copy", category="技术文档", _offset=1)
  → 第2个文档入库完成，下一个又是文档需要分类
  → 返回 success + 进度信息 + 下一个是文档需要分类

子Agent判断分类，调用 ingest(dir_path, mode="copy", category="工作文档", _offset=2)
  → 第3个文档入库完成，后面都是图片不需要分类
  → 继续处理剩余图片...

子Agent调用 ingest(dir_path, mode="copy", _offset=3)
  → 剩余图片逐个入库
  → 返回最终汇总结果（所有文件处理完毕）
```

**关键**：
- 不是重新执行，ingest 内部记住处理进度（_offset 参数）
- 照片不需要分类，遇到照片直接入库
- 文档需要分类时暂停，返回 need_category 让子Agent判断
- 子Agent判断分类后继续调用，ingest 从上次进度继续处理
- 所有文件处理完毕后返回最终汇总结果

## ingest 工具行为定义

### 路径是文件

正常入库，返回入库结果。不走循环逻辑。

### 路径是目录 — 分页式处理

ingest 收到目录路径时，扫描目录获取所有文件列表，按顺序逐个处理。

每次调用处理到"需要子Agent决策"的节点时暂停，返回当前结果和下一步信息。

#### 返回值格式

**遇到图片（直接入库）**：
```python
{
    "status": "progress",
    "processed": 1,
    "total": 25,
    "last_result": {"file": "photo1.jpg", "status": "success", "category": "照片", ...},
    "next": {"file": "doc1.pdf", "type": "document", "needs_category": True},
    "message": "已处理 1/25，下一文件 doc1.pdf 需要分类"
}
```

**遇到文档且 category 已指定**：
```python
{
    "status": "progress",
    "processed": 3,
    "total": 25,
    "last_result": {"file": "doc1.pdf", "status": "success", "category": "技术文档", ...},
    "next": {"file": "doc2.pdf", "type": "document", "needs_category": True},
    "message": "已处理 3/25，下一文件 doc2.pdf 需要分类"
}
```

**遇到文档且 category 未指定**：
```python
{
    "status": "need_category",
    "processed": 3,
    "total": 25,
    "current_file": "doc1.pdf",
    "preview": "文档内容前20000字符...",
    "available_categories": ["技术文档", "工作文档", "个人资料", "学习笔记"],
    "message": "请从 available_categories 中选择分类后继续调用"
}
```

**所有文件处理完毕**：
```python
{
    "status": "success",
    "total": 25,
    "photos": 18,
    "documents": 5,
    "skipped": 2,
    "details": {
        "photos": [...],
        "documents": [...],
        "skipped": [".DS_Store", "thumbs.db"]
    }
}
```

### _offset 参数 — 进度记忆

ingest 通过 `_offset` 参数记住上次处理到第几个文件。每次调用时：
- `_offset=0`（默认）：从头开始
- `_offset=N`：跳过已处理的N个文件，从第N+1个开始

已入库的文件信息存储在临时状态中（DB或临时文件），ingest 内部读取之前的处理记录，不需要子Agent重复传递。

## 文件处理细节

### classify_path(path) — 路径分类

判断路径是图片还是文档：
- 图片后缀：.jpg .jpeg .png .heic .webp .bmp .tiff .gif
- 文档后缀：.pdf .doc .docx .ppt .pptx .xls .xlsx .md .html .txt .epub
- 其他后缀：跳过，记录到 skipped 列表
- 隐藏文件（.开头）：跳过
- 子目录：递归扫描

### 三种入库模式

| 模式 | 文件操作 | DB记录source_path | 说明 |
|------|----------|-------------------|------|
| copy | 复制到 `workspace/year/category/filename` | 目标路径 | 默认模式 |
| move | 移动到 `workspace/year/category/filename` | 目标路径 | 原文件消失 |
| reference | 不移动 | 原始路径 | 原地保留，只建KG |

### year 取值规则

- 照片：优先取EXIF拍摄时间（DateTimeOriginal），无EXIF取文件修改时间
- 文档：取文件修改时间

### 目标路径冲突

目标路径已存在同名文件时：追加数字后缀（如 `photo_1.jpg`、`photo_2.jpg`）

## EXIF信息写入KG

### 当前问题

`ingest_photo()` 调用链：
```
extract_exif() → {taken_at, location, camera} → 存入DB
sync_photo_to_kg() → format_photo_ingest_data() → _generate_stable_description()
                                                       ↓
                                              只用文件名提取日期
                                              不含 camera / location
```

### 修复方案

在 `sync_photo_to_kg()` 调用链中传入 EXIF 数据：

1. `ingest_photo()` 调 `sync_photo_to_kg()` 时传入 `exif=exif`
2. `sync_photo_to_kg()` → `_do_sync_photo_to_kg()` → `format_photo_ingest_data()` 传入 `exif=exif`
3. `_generate_stable_description()` 接收 exif 参数，格式化写入 description

### description 格式

```
照片 20260419_143000，拍摄于2026年04月19日 14:30，设备：Apple iPhone 15 Pro，位置：31.23,121.47
```

无EXIF时 fallback：
```
照片 20260419_143000，拍摄于2026年04月19日
```

## 子Agent提示词约束

file-processor 子Agent必须理解 ingest 工具的循环调用规范，防止中途退出或格式错误。

### 核心约束

1. **ingest 工具会多轮返回**——这是正常的工具循环，不是错误。你必须持续响应直到收到最终结果。

2. **收到 `status: "need_category"` 时**——这是工具在问你问题。你必须：
   - 阅读预览内容
   - 从 `available_categories` 列表中选择一个分类
   - **只回答分类名称**，不要加解释、不要加标点、不要加引号
   - 格式错误会导致工具反复问你同一个问题

3. **收到 `status: "progress"` 时**——这是中间进度，继续调用 ingest 处理下一个文件。

4. **收到 `status: "success"` 且包含 `total` 汇总字段时**——这才是工具循环结束的标志。只有看到这个结果，你才能向主Agent汇报入库完成。

5. **禁止提前退出**——在没有收到最终汇总结果之前，不能向主Agent汇报"入库完成"。部分文件入库不等于完成。

### 正确示例

```
工具返回: {"status": "need_category", "current_file": "报告.pdf", "available_categories": ["技术文档", "工作文档", "个人资料"]}
Agent回答: 技术文档                              ← 正确：只回答分类名

工具返回: {"status": "need_category", "current_file": "简历.docx", "available_categories": ["技术文档", "工作文档", "个人资料"]}
Agent回答: 个人资料                              ← 正确：只回答分类名

工具返回: {"status": "success", "total": 5, "photos": 3, "documents": 2}
Agent向主Agent汇报: 入库完成，3张照片和2个文档已入库  ← 正确：看到最终汇总才汇报
```

### 错误示例

```
工具返回: {"status": "need_category", "current_file": "报告.pdf", "available_categories": ["技术文档", "工作文档", "个人资料"]}
Agent回答: 我觉得这个应该放在技术文档里    ← 错误：多了多余文字，工具可能无法识别

工具返回: {"status": "progress", "processed": 3, "total": 10}
Agent向主Agent汇报: 入库完成               ← 错误：还没处理完就汇报了
```

## 需要修改的文件

| 文件 | 修改内容 |
|------|----------|
| `photo-server/__init__.py` | ingest() 支持目录分页处理、ingest_photo() 加mode、EXIF写入KG |
| `photo-server/TOOL_SCHEMAS` | 更新 ingest 的参数定义（加 _offset） |
| `config/mcp-servers.yaml` | 更新 photo-server 的工具配置 |
| `config/disk/` | 更新 disk YAML |
| `config/agents/file-processor.md` | 子Agent提示词：理解 progress/need_category/success 三种状态 |

## 不需要修改的

- `ingest_document()` — 单文档入库流程完整，只需确保 mode 参数传入
- `sync_photo_to_kg()` — 只需增加 EXIF 参数传入
- `ToolRegistry.ask_agent` — 本方案不使用
- `MCPClientManager / Sampling` — 本方案不使用