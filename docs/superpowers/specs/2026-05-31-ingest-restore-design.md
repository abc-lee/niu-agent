# 文档/照片入库功能恢复设计

## 问题

原始 `ingest_unified.py` 实现了完整的统一入库：一个入口自动判断路径类型（文件/目录）和内容类型（照片/文档/混合），支持 copy/move/reference 三种模式。在后续重构为 MCP photo-server 时，大量功能丢失：

| 功能 | 原始 | 当前 | 状态 |
|------|------|------|------|
| 路径+内容类型自动判断 | classify_path() | 无 | 丢失 |
| 照片 copy/move/reference | 三分支 | 硬编码 copy | 丢失 |
| 文档 copy/move/reference | 三分支 | 三分支 | 正常 |
| 批量照片完整处理 | 逐张 ingest_photo() | 仅 shutil.copy2 | 丢失 |
| 批量照片 mode 参数 | 传递 mode | 无 | 丢失 |
| 纯文档目录入库 | _ingest_document_directory() | DIRECTORY_NO_PHOTOS | 丢失 |
| 混合目录入库 | _ingest_mixed_directory() | 只处理照片部分 | 丢失 |
| move 模式错误回滚 | shutil.move 回原位 | os.remove 丢文件 | 丢失 |
| 统一入口路由 | ingest() 自动分发 | 委托给 ingest_document | 丢失 |

## 设计

### 核心思路

恢复 `ingest` 工具为真正的统一入口：接收 path + mode + category，自动判断路径类型和内容类型，路由到正确的处理分支。所有底层函数复用现有代码，只补缺失部分。

### 架构

```
ingest(path, mode, category)
    │
    ├── classify_path(path)
    │   ├── FILE + PHOTO  → ingest_photo(file_path, mode, category)
    │   ├── FILE + DOCUMENT → ingest_document(file_path, mode, category)
    │   ├── DIRECTORY + PHOTO → ingest_photo_directory(path, mode, category)
    │   ├── DIRECTORY + DOCUMENT → ingest_document_directory(path, mode, category)
    │   ├── DIRECTORY + MIXED → ingest_mixed_directory(path, mode, category)
    │   └── DIRECTORY + EMPTY → 报错
    │
    └── 各分支内部处理
        ├── ingest_photo() — 加 mode 参数 + 修复回滚
        ├── ingest_document() — 已有 mode，保持不变
        ├── ingest_photo_directory() — 新增，逐张调 ingest_photo()
        ├── ingest_document_directory() — 新增，逐个调 ingest_document()
        └── ingest_mixed_directory() — 新增，分别调上面两个
```

### 改动1：新增路径分类函数

从 `ingest_unified.py:28-72` 移植 `PathType`、`ContentType` 枚举和 `classify_path()` 函数到 `__init__.py`。逻辑不变，只是移到新位置。

```python
class PathType(Enum):
    FILE = "file"
    DIRECTORY = "directory"
    NOT_FOUND = "not_found"

class ContentType(Enum):
    PHOTO = "photo"
    DOCUMENT = "document"
    MIXED = "mixed"
    EMPTY = "empty"

def classify_path(path: str) -> tuple[PathType, ContentType]:
    source = Path(path)
    if not source.exists():
        return PathType.NOT_FOUND, ContentType.EMPTY
    if source.is_file():
        ext = source.suffix.lower()
        if ext in PHOTO_EXTENSIONS:
            return PathType.FILE, ContentType.PHOTO
        return PathType.FILE, ContentType.DOCUMENT
    # directory
    photos = sum(1 for f in source.rglob("*") if f.is_file() and f.suffix.lower() in PHOTO_EXTENSIONS)
    docs = sum(1 for f in source.rglob("*") if f.is_file() and f.suffix.lower() in DOCUMENT_EXTENSIONS and f.suffix.lower() not in PHOTO_EXTENSIONS)
    if photos > 0 and docs > 0:
        return PathType.DIRECTORY, ContentType.MIXED
    if photos > 0:
        return PathType.DIRECTORY, ContentType.PHOTO
    if docs > 0:
        return PathType.DIRECTORY, ContentType.DOCUMENT
    return PathType.DIRECTORY, ContentType.EMPTY
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
    # rollback file operation
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

### 改动3：重写批量照片入库为完整处理

删除 `ingest_photos_batch()` 的"只拷贝"逻辑，改为新增 `_ingest_photo_directory()`，逐张调用 `ingest_photo()`：

```python
def _ingest_photo_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """照片目录入库：逐张走完整流程（EXIF/人脸/KG/DB）"""
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
        "errors": errors[:10],  # 最多返回10个错误
        "photos": results,
    }
```

同步更新 `ingest_photos()` 签名加 `mode`，透传给 `_ingest_photo_directory` 或 `ingest_photo`。

### 改动4：新增文档目录入库

```python
def _ingest_document_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """文档目录入库：逐个调用 ingest_document()，未指定分类时首个 need_category 即中断"""
    source = Path(source_path)
    doc_files = sorted([f for f in source.rglob("*")
                       if f.is_file() and f.suffix.lower() in DOCUMENT_EXTENSIONS
                       and f.suffix.lower() not in PHOTO_EXTENSIONS])

    results = []
    errors = []

    for df in doc_files:
        # 跳过已入库的文件（通过 hash 检测）
        file_hash = _compute_file_hash(str(df))
        existing = _check_document_exists(file_hash)
        if existing:
            results.append({"status": "skipped", "file": str(df), "reason": "already_ingested"})
            continue

        result = ingest_document(str(df), mode=mode, category=category)
        if result.get("status") == "success":
            results.append(result)
        elif result.get("status") == "need_category":
            # 未指定分类时，第一个 need_category 即中断，返回给子Agent选择分类
            return {
                "status": "need_category",
                "total": len(doc_files),
                "succeeded": len(results),
                "pending": len(doc_files) - len(results) - 1,
                "current_file": result,
                "message": f"已入库 {len(results)} 个文档，还有 {len(doc_files) - len(results) - 1} 个待处理。请为当前文档选择分类。",
            }
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

当 `category` 已指定时，所有文档直接入库，`need_category` 为空。
当 `category` 未指定时，逐个调用 `ingest_document()`，第一个返回 `need_category` 的文件会中断批量流程——工具一次性返回该文件的内容预览 + `available_categories`，子Agent判断分类后带 category 重新调用整个目录。这意味着未指定 category 时，目录入库需要两轮：第一轮碰到第一个需要分类的文件就返回，子Agent选好分类后第二轮用 category 重新调用。已入库的文档不会重复处理（通过 hash 检测跳过）。

### 改动5：新增混合目录入库

```python
def _ingest_mixed_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """混合目录入库：照片和文档分别处理"""
    photo_result = _ingest_photo_directory(source_path, mode=mode, category=category)
    doc_result = _ingest_document_directory(source_path, mode=mode, category=category)

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

    path_type, content_type = classify_path(path)

    if path_type == PathType.NOT_FOUND:
        return {"status": "error", "message": f"路径不存在: {path}"}

    if path_type == PathType.FILE:
        if content_type == ContentType.PHOTO:
            return ingest_photo(path, mode=mode, category=category)
        else:
            return ingest_document(path, mode=mode, category=category)

    # DIRECTORY
    if content_type == ContentType.PHOTO:
        return _ingest_photo_directory(path, mode=mode, category=category)
    elif content_type == ContentType.DOCUMENT:
        return _ingest_document_directory(path, mode=mode, category=category)
    elif content_type == ContentType.MIXED:
        return _ingest_mixed_directory(path, mode=mode, category=category)
    else:  # EMPTY
        return {"status": "error", "message": f"目录为空: {path}"}
```

### 改动7：更新 TOOL_SCHEMAS 和 MCP Tool schema

- `ingest` schema 的 description 更新为准确描述路由行为
- `ingest_photo` schema 加 `mode` 参数
- `ingest_photos` schema 加 `mode` 参数

### 改动8：更新 file-processor 子Agent 提示词

`config/agents/file-processor.md` 中照片入库指令需要同步更新：
- 照片入库现在也支持 mode 参数
- 目录入库自动识别内容类型，不需要手动区分

## 不改动

- `ingest_document()` 单文件逻辑：保持现有实现（mode 三分支 + need_category + lightrag_insert_file）
- `ingest_documents()` 批量函数：保持现有签名
- 底层辅助函数：`extract_exif`, `detect_faces`, `match_face_to_person`, `generate_l0_abstract`, `sync_photo_to_kg` 等全部保持不变
- 数据库表结构：无变化
- `store_document_l1()` / L1 回传模式：暂不恢复，当前 `lightrag_insert_file` 路径已满足需求

## 文件清单

| 文件 | 改动 | 风险 |
|------|------|------|
| `__init__.py` | 新增 classify_path + 3个目录入库函数 + 重写 ingest 路由 + ingest_photo 加 mode | 中 |
| `__init__.py` MCP Tool schemas | ingest_photo/ingest_photos 加 mode 参数 | 低 |
| `__init__.py` TOOL_SCHEMAS | ingest 描述更新 + photo schemas 加 mode | 低 |
| `config/agents/file-processor.md` | 照片入库指令更新 | 低 |

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
- 数据库记录写入
- 人脸检测/匹配正常（照片）
- 知识图谱写入正常（文档）
- move 模式下源文件已删除
- reference 模式下不复制文件