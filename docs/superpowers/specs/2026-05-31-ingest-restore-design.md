# 文档/照片入库功能恢复设计

## 问题

原始 `ingest_unified.py` 实现了完整的统一入库：一个入口自动判断路径类型和内容类型，支持 copy/move/reference 三种模式。重构为 MCP photo-server 时大量功能丢失：

| 功能 | 原始 | 当前 | 状态 |
|------|------|------|------|
| 路径+内容类型自动判断 | classify_path() | 无 | 丢失 |
| 照片 copy/move/reference | 三分支 | 硬编码 copy | 丢失 |
| 文档 copy/move/reference | 三分支 | 三分支 | 正常 |
| 批量照片完整处理 | 逐张 ingest_photo() | 仅 shutil.copy2 | 丢失 |
| 纯文档目录入库 | 逐个调 ingest_document() | DIRECTORY_NO_PHOTOS | 丢失 |
| 混合目录入库 | 照片/文档分别处理 | 只处理照片部分 | 丢失 |
| move 模式错误回滚 | shutil.move 回原位 | os.remove 丢文件 | 丢失 |

## 工具交互机制

### MCP 工具是请求-响应模式

每次 `ingest_document()` 调用都是独立的、无状态的函数调用。子Agent和工具之间通过子Agent的 LLM 循环协调：

```
子Agent LLM 循环：
  → 调用 ingest_document(file_path="a.pdf")          ← 第1次工具调用
  ← 返回 need_category + 内容预览 + available_categories
  → 子Agent阅读预览，判断分类为"报告"
  → 调用 ingest_document(file_path="a.pdf", category="报告")  ← 第2次工具调用
  ← 返回 success
  → 调用 ingest_document(file_path="b.docx")          ← 第3次工具调用
  ← ...
```

**关键**：每次工具调用返回后，子Agent的 LLM 决定下一步操作。photo-server 本身没有内部循环，不维护跨调用的状态。

### 目录入库的工作方式

当用户拖入一个含10个文档的目录且未指定分类时，子Agent需要为每个文件做两轮调用（拿预览 → 带分类入库），共约20次工具调用。这些调用全部由子Agent的 LLM 循环驱动。

如果用户指定了分类，则每个文件只需一次调用（直接带分类入库），共约10次。

### 分类目录来自配置文件

`available_categories` 从 `~/.niu/preferences.json` 的 `categories.documents` 字段读取，用户可自定义。

## 设计

### 核心思路

1. 恢复 `ingest` 工具为统一入口：接收 path + mode + category，自动判断内容类型，路由到正确分支
2. `ingest_photo()` 加 mode 参数 + 修复 move 回滚
3. 批量照片改为逐张调 `ingest_photo()`（完整流程）
4. 新增文档目录入库函数
5. 新增混合目录入库函数
6. 子Agent提示词同步更新

### 架构

```
ingest(path, mode, category)
    │
    ├── classify_path(path)
    │   ├── FILE + PHOTO     → ingest_photo(path, mode, category)
    │   ├── FILE + DOCUMENT → ingest_document(path, mode, category)
    │   ├── DIR + PHOTO     → ingest_photo_directory(path, mode, category)
    │   ├── DIR + DOCUMENT  → ingest_document_directory(path, mode, category)
    │   ├── DIR + MIXED     → ingest_mixed_directory(path, mode, category)
    │   └── DIR + EMPTY     → error
    │
    └── 底层函数（已有，复用）
        ├── ingest_photo()        — 加 mode + 修复回滚
        ├── ingest_document()    — 保持不变
        ├── extract_exif()       — 不变
        ├── detect_faces()       — 不变
        ├── match_face_to_person() — 不变
        ├── sync_photo_to_kg()   — 不变
        └── lightrag_insert_file() — 不变
```

### 改动1：新增路径分类函数

从 `ingest_unified.py:28-72` 移植。不改变逻辑。

```python
class ContentType(Enum):
    PHOTO = "photo"
    DOCUMENT = "document"
    MIXED = "mixed"
    EMPTY = "empty"

def classify_path(path: str) -> ContentType:
    source = Path(path)
    if source.is_file():
        if source.suffix.lower() in PHOTO_EXTENSIONS:
            return ContentType.PHOTO
        return ContentType.DOCUMENT
    # directory
    has_photo = any(f.is_file() and f.suffix.lower() in PHOTO_EXTENSIONS for f in source.rglob("*"))
    has_doc = any(f.is_file() and f.suffix.lower() in DOCUMENT_EXTENSIONS and f.suffix.lower() not in PHOTO_EXTENSIONS for f in source.rglob("*"))
    if has_photo and has_doc:
        return ContentType.MIXED
    if has_photo:
        return ContentType.PHOTO
    if has_doc:
        return ContentType.DOCUMENT
    return ContentType.EMPTY
```

### 改动2：ingest_photo() 加 mode 参数 + 修复回滚

**签名变更**：
```python
def ingest_photo(file_path: str, mode: str = "copy", category: str | None = None) -> dict:
```

**文件操作三分支**（参照 `ingest_unified.py:175-180`）：
```python
if mode == "copy":
    shutil.copy2(str(source), final_path)
elif mode == "move":
    shutil.move(str(source), final_path)
elif mode == "reference":
    final_path = str(source)
```

**错误回滚修复**（参照 `ingest_unified.py:216-231`）：
```python
except Exception as e:
    if final_path is not None:
        try:
            if mode == "move":
                shutil.move(str(final_path), str(source))
            elif mode != "reference":
                if os.path.exists(final_path):
                    os.remove(final_path)
        except OSError:
            pass
```

### 改动3：批量照片改为逐张完整处理

新增 `ingest_photo_directory()`，逐张调用已有的 `ingest_photo()`（已包含 EXIF/人脸/KG/DB 完整流程）。删除 `ingest_photos_batch()` 的"只拷贝"逻辑。

```python
def ingest_photo_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """照片目录入库：逐张走完整流程"""
    source = Path(source_path)
    photo_files = sorted([f for f in source.rglob("*") if f.is_file() and f.suffix.lower() in PHOTO_EXTENSIONS])

    results = []
    errors = []
    for pf in photo_files:
        result = ingest_photo(str(pf), mode=mode, category=category)
        if result.get("status") == "success":
            results.append(result)
        else:
            errors.append({"file": str(pf), "error": result.get("message", "unknown")})

    return {
        "status": "success",
        "total": len(photo_files),
        "succeeded": len(results),
        "failed": len(errors),
        "errors": errors[:10],
        "photos": results,
    }
```

同步更新 `ingest_photos()` 签名加 `mode`，路由到 `ingest_photo` 或 `ingest_photo_directory`。

### 改动4：新增文档目录入库

逐个调用已有的 `ingest_document()`。子Agent通过工具调用循环来处理 need_category 交互——程序不需要内部循环。

```python
def ingest_document_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """文档目录入库：逐个调用 ingest_document()"""
    source = Path(source_path)
    doc_files = sorted([f for f in source.rglob("*")
                       if f.is_file() and f.suffix.lower() in DOCUMENT_EXTENSIONS
                       and f.suffix.lower() not in PHOTO_EXTENSIONS])

    results = []
    errors = []
    for df in doc_files:
        result = ingest_document(str(df), mode=mode, category=category or "")
        if result.get("status") == "success":
            results.append(result)
        else:
            errors.append({"file": str(df), "error": result.get("message", "unknown")})

    return {
        "status": "success",
        "total": len(doc_files),
        "succeeded": len(results),
        "failed": len(errors),
        "errors": errors[:10],
        "documents": results,
    }
```

注意：当 `category` 为空时，`ingest_document()` 会对每个文件返回 `need_category`。这些结果会被收集到 errors 中（因为 status 不是 success）。子Agent看到这些信息后，会逐个带分类重新调用工具。

### 改动5：新增混合目录入库

```python
def ingest_mixed_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """混合目录入库：照片和文档分别处理"""
    photo_result = ingest_photo_directory(source_path, mode=mode, category=category)
    doc_result = ingest_document_directory(source_path, mode=mode, category=category)

    return {
        "status": "success",
        "photos": photo_result,
        "documents": doc_result,
    }
```

### 改动6：重写 ingest 工具路由

`call_tool` 中 `name == "ingest"` 分支重写：

```python
if name == "ingest":
    path = arguments["path"]
    mode = arguments.get("mode", "copy")
    category = arguments.get("category", "") or None

    content_type = classify_path(path)

    if content_type == ContentType.EMPTY:
        return {"status": "error", "message": f"目录为空或路径不存在: {path}"}

    source = Path(path)
    if source.is_file():
        if content_type == ContentType.PHOTO:
            return ingest_photo(path, mode=mode, category=category)
        else:
            return ingest_document(path, mode=mode, category=category)

    # DIRECTORY
    if content_type == ContentType.PHOTO:
        return ingest_photo_directory(path, mode=mode, category=category)
    elif content_type == ContentType.DOCUMENT:
        return ingest_document_directory(path, mode=mode, category=category)
    else:  # MIXED
        return ingest_mixed_directory(path, mode=mode, category=category)
```

### 改动7：更新 TOOL_SCHEMAS 和 MCP Tool schemas

- `ingest` schema description 更新为准确描述路由行为
- `ingest_photo` schema 加 `mode` 参数 (enum: copy/move/reference, default: copy)
- `ingest_photos` schema 加 `mode` 参数

### 改动8：更新 file-processor 子Agent 提示词

`config/agents/file-processor.md` 同步更新：
- 照片入库现在也支持 mode 参数
- 目录入库自动识别内容类型（照片/文档/混合）
- 保持现有的 need_category 交互流程不变

## 不改动

- `ingest_document()` 单文件逻辑：保持不变
- `ingest_documents()` 批量函数：保持现有签名
- 底层辅助函数：全部保持不变
- 数据库表结构：无变化
- `store_document_l1()` / L1 回传模式：暂不恢复
- 分类目录来源：仍从 `preferences.json` 读取

## 文件清单

| 文件 | 改动 | 风险 |
|------|------|------|
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | classify_path + 目录入库函数 + ingest 路由重写 + ingest_photo 加 mode | 中 |
| `config/agents/file-processor.md` | 照片 mode 参数 + 目录入库指令 | 低 |

## 测试计划

启动应用后做真实测试，6种场景 x 3种模式 = 18个测试：

| 场景 | copy | move | reference |
|------|------|------|-----------|
| 单张照片 | ✓ | ✓ | ✓ |
| 单个文档 | ✓ | ✓ | ✓ |
| 纯照片目录 | ✓ | ✓ | ✓ |
| 纯文档目录 | ✓ | ✓ | ✓ |
| 混合目录 | ✓ | ✓ | ✓ |
| 空目录 | 报错 | 报错 | 报错 |

每个测试验证：
- 文件存储位置正确
- 数据库记录写入（照片）
- 人脸检测/匹配正常（照片）
- 知识图谱写入正常（文档）
- move 模式下源文件已删除
- reference 模式下不复制文件
- move 模式错误时文件正确回滚