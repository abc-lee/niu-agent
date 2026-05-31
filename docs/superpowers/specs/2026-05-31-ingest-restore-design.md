# 文档/照片入库功能恢复设计

## 设计原则

**程序尽可能自己处理，处理不了才问Agent。**

用户拖入路径后，程序负责所有自动判断和处理。只在"文档需要分类且用户未指定"这一个场景下，才返回问Agent。照片部分永远不需要问Agent，程序自己全部处理完。

## 完整流程

```
用户拖入 path
    │
    ▼
程序判断 path 类型
    │
    ├── 不存在 → 返回 error
    ├── 文件 → 判断照片/文档
    ├── 目录 → 扫描内容，判断纯照片/纯文档/混合/空
    │
    ▼
照片部分（永远不需要问Agent）
    │
    ├── 逐张走完整流程：EXIF → 人脸检测 → 人物匹配 → DB写入 → KG同步
    ├── mode 参数：copy/move/reference
    └── 全部处理完，收集结果

文档部分
    │
    ├── Agent 已指定 category → 逐个直接入库（拷贝+KG），不问
    ├── Agent 未指定 category → 逐个处理：
    │   ├── 读取内容（≤20K），返回 need_category + 预览 + available_categories
    │   │   → Agent 选择分类后再次调用 → 继续处理剩余文件
    │   ├── 文件不支持 KG → 仍然拷贝到知识库目录，标记 lightrag=unsupported
    │   └── 全部处理完，收集结果
    │
    ▼
返回完整结果（照片结果 + 文档结果）
```

## 当前问题

| 功能 | 原始设计 | 当前实现 | 问题 |
|------|----------|----------|------|
| 路径类型自动判断 | classify_path() 扫描 | 无 | 目录入库直接报错 |
| 照片 mode 参数 | copy/move/reference | 硬编码 copy | move/reference 失效 |
| 批量照片完整处理 | 逐张 ingest_photo() | ingest_photos_batch() 只做 copy | 无人脸/EXIF/KG/DB |
| 纯文档目录入库 | 逐个调 ingest_document() | 返回 DIRECTORY_NO_PHOTOS | 完全无法入库 |
| 混合目录入库 | 照片/文档分别处理 | 只处理照片部分 | 文档被忽略 |
| move 模式错误回滚 | shutil.move 回原位 | os.remove 删文件 | 数据丢失 |

## 工具交互机制

MCP 工具是请求-响应模式，每次调用独立无状态。子Agent 通过 LLM 循环协调多次工具调用：

- **照片目录**：一次调用，程序内部循环逐张处理，返回完整结果。子Agent不需要多次调用。
- **文档目录（有 category）**：一次调用，程序内部循环逐个入库，返回完整结果。
- **文档目录（无 category）**：第一次调用，程序逐个处理，碰到第一个需要分类的文件返回 need_category → 子Agent选择分类 → 第二次调用带 category，程序继续处理（已入库的通过 hash 跳过）→ 如还有未分类的，再次返回 need_category → 循环直到全部完成。

## 改动

### 改动1：新增路径分类

从 `ingest_unified.py:28-72` 移植逻辑。

```python
class ContentType(Enum):
    PHOTO = "photo"
    DOCUMENT = "document"
    MIXED = "mixed"
    EMPTY = "empty"

def classify_path(path: str) -> ContentType:
    """判断路径内容类型。文件直接看扩展名，目录扫描子文件统计。"""
    source = Path(path)
    if not source.exists():
        return ContentType.EMPTY
    if source.is_file():
        return ContentType.PHOTO if source.suffix.lower() in PHOTO_EXTENSIONS else ContentType.DOCUMENT
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

新增 `ingest_photo_directory()`，程序内部循环逐张调用 `ingest_photo()`。一次调用返回完整结果，不需要子Agent多次调用。

```python
def ingest_photo_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """照片目录入库：程序内部逐张走完整流程，一次返回全部结果。"""
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

程序内部循环逐个调用 `ingest_document()`。

- **有 category**：所有文件直接入库，一次返回完整结果。
- **无 category**：逐个处理，第一个需要分类的文件返回 need_category。子Agent选择分类后带 category 重新调用。已入库的文件通过 hash 检测自动跳过。

```python
def ingest_document_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """文档目录入库：程序逐个处理，需要分类时返回问Agent。"""
    source = Path(source_path)
    doc_files = sorted([f for f in source.rglob("*")
                       if f.is_file() and f.suffix.lower() in DOCUMENT_EXTENSIONS
                       and f.suffix.lower() not in PHOTO_EXTENSIONS])

    results = []
    errors = []

    for df in doc_files:
        result = ingest_document(str(df), mode=mode, category=category or "")

        if result.get("status") == "success" or result.get("status") == "skipped":
            results.append(result)
        elif result.get("status") == "need_category":
            # 程序处理不了，返回给Agent选择分类
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

### 改动5：新增混合目录入库

照片部分程序自己全部处理完，文档部分按改动4的逻辑处理。

```python
def ingest_mixed_directory(source_path: str, mode: str = "copy", category: str | None = None) -> dict:
    """混合目录入库：照片程序处理完，文档按需问Agent。"""
    # 照片部分：程序内部全部处理完，不需要问Agent
    photo_result = ingest_photo_directory(source_path, mode=mode, category=category)

    # 文档部分：有category则一次处理完，没有则按需问Agent
    doc_result = ingest_document_directory(source_path, mode=mode, category=category)

    # 如果文档部分返回 need_category，整体也返回 need_category
    if doc_result.get("status") == "need_category":
        return {
            "status": "need_category",
            "photos": photo_result,
            "current_file": doc_result["current_file"],
            "total_docs": doc_result["total"],
            "succeeded_docs": doc_result["succeeded"],
            "pending_docs": doc_result["pending"],
            "message": f"照片已全部入库({photo_result['succeeded']}张)。文档还需要分类：{doc_result['message']}",
        }

    return {
        "status": "success",
        "photos": photo_result,
        "documents": doc_result,
    }
```

### 改动6：重写 ingest 工具路由

`call_tool` 中 `name == "ingest"` 分支：

```python
if name == "ingest":
    path = arguments["path"]
    mode = arguments.get("mode", "copy")
    category = arguments.get("category", "") or None

    content_type = classify_path(path)

    if content_type == ContentType.EMPTY:
        return {"status": "error", "message": f"路径不存在或目录为空: {path}"}

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

- `ingest` schema description 更新：明确说明程序自动判断内容类型，照片自动处理，文档需要分类
- `ingest_photo` schema 加 `mode` 参数 (enum: copy/move/reference, default: copy)
- `ingest_photos` schema 加 `mode` 参数

### 改动8：更新 file-processor 子Agent 提示词

`config/agents/file-processor.md`：
- 照片入库：现在也支持 mode 参数
- 目录入库：程序自动识别内容类型，照片自动处理完
- 文档分类：need_category 交互流程保持不变
- 混合目录：照片自动处理，文档按需问分类

## 不改动

- `ingest_document()` 单文件逻辑：保持不变
- `ingest_documents()` 批量函数：保持现有签名
- 底层辅助函数：全部保持不变
- 数据库表结构：无变化
- 分类目录来源：仍从 `preferences.json` 读取
- L1 回传模式：暂不恢复

## 文件清单

| 文件 | 改动 |
|------|------|
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | classify_path + 目录入库函数 + ingest 路由重写 + ingest_photo 加 mode |
| `config/agents/file-processor.md` | 提示词更新 |

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