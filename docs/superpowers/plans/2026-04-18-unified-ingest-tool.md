# 统一入库工具 (ingest) 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 4 个入库工具合并为 1 个 `ingest` 工具，自动判断路径/内容类型，与子 Agent 形成 L1 生成循环

**Architecture:** 在 `scripts/ingest_unified.py` 独立开发，复用 photo-server 内部函数（extract_exif、detect_faces、match_face_to_person、sync_photo_to_kg 等），TDD 驱动，跑通后替换 photo-server 中的 4 个工具

**Tech Stack:** Python 3.11+, SQLite, InsightFace, SentenceTransformer, KuzuDB

**测试资源:**
- 照片目录: `E:\tmp\2009.6.4西柏坡` (33 张 JPG)
- 文档: `docs/SYSTEM_MANUAL.md`, `docs/USAGE-self-evolution.md`
- LLM 配置: `config/user-config.json`

---

### Task 1: 搭建测试骨架 + 路径类型判断

**Files:**
- Create: `scripts/ingest_unified.py`
- Create: `tests/test_ingest_unified.py`

- [ ] **Step 1: 写失败测试 — 路径类型判断**

```python
# tests/test_ingest_unified.py
import pytest
from pathlib import Path
from ingest_unified import classify_path, PathType, ContentType

class TestClassifyPath:
    def test_single_photo(self):
        result = classify_path(Path("E:/tmp/2009.6.4西柏坡/DSC_3272.jpg"))
        assert result.path_type == PathType.FILE
        assert result.content_type == ContentType.PHOTO

    def test_single_document(self):
        result = classify_path(Path("docs/SYSTEM_MANUAL.md"))
        assert result.path_type == PathType.FILE
        assert result.content_type == ContentType.DOCUMENT

    def test_photo_directory(self):
        result = classify_path(Path("E:/tmp/2009.6.4西柏坡"))
        assert result.path_type == PathType.DIRECTORY
        assert result.content_type == ContentType.PHOTO

    def test_nonexistent(self):
        result = classify_path(Path("E:/tmp/nonexistent"))
        assert result.path_type == PathType.NOT_FOUND

    def test_mixed_directory(self, tmp_path):
        (tmp_path / "photo.jpg").touch()
        (tmp_path / "report.pdf").touch()
        result = classify_path(tmp_path)
        assert result.path_type == PathType.DIRECTORY
        assert result.content_type == ContentType.MIXED
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_ingest_unified.py::TestClassifyPath -v`
Expected: FAIL (module not found)

- [ ] **Step 3: 实现最小代码**

```python
# scripts/ingest_unified.py
from enum import Enum
from pathlib import Path

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif"}

class PathType(Enum):
    FILE = "file"
    DIRECTORY = "directory"
    NOT_FOUND = "not_found"

class ContentType(Enum):
    PHOTO = "photo"
    DOCUMENT = "document"
    MIXED = "mixed"
    EMPTY = "empty"

class PathInfo:
    def __init__(self, path_type: PathType, content_type: ContentType):
        self.path_type = path_type
        self.content_type = content_type

def classify_path(path: Path) -> PathInfo:
    if not path.exists():
        return PathInfo(PathType.NOT_FOUND, ContentType.EMPTY)
    if path.is_file():
        ext = path.suffix.lower()
        if ext in PHOTO_EXTENSIONS:
            return PathInfo(PathType.FILE, ContentType.PHOTO)
        return PathInfo(PathType.FILE, ContentType.DOCUMENT)
    if path.is_dir():
        photos = 0
        docs = 0
        for f in path.rglob("*"):
            if f.is_file():
                if f.suffix.lower() in PHOTO_EXTENSIONS:
                    photos += 1
                else:
                    docs += 1
        if photos > 0 and docs > 0:
            return PathInfo(PathType.DIRECTORY, ContentType.MIXED)
        if photos > 0:
            return PathInfo(PathType.DIRECTORY, ContentType.PHOTO)
        if docs > 0:
            return PathInfo(PathType.DIRECTORY, ContentType.DOCUMENT)
        return PathInfo(PathType.DIRECTORY, ContentType.EMPTY)
    return PathInfo(PathType.NOT_FOUND, ContentType.EMPTY)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd E:/tools/ai-bot && python -m pytest tests/test_ingest_unified.py::TestClassifyPath -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/ingest_unified.py tests/test_ingest_unified.py
git commit -m "feat: unified ingest tool — path classification (TDD step 1)"
```

---

### Task 2: 单张照片入库

**Files:**
- Modify: `scripts/ingest_unified.py`
- Modify:- Modify: `tests/test_ingest_unified.py`

- [ ] **Step 1: 写失败测试 — 单张照片入库**

```python
class TestIngestPhoto:
    def test_single_photo_ingest(self):
        result = ingest(path="E:/tmp/2009.6.4西柏坡/DSC_3272.jpg")
        assert result["status"] == "success"
        assert "photo_id" in result
        assert "detected_persons" in result
        assert "file_path" in result
        assert Path(result["file_path"]).exists()
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现 ingest() — 单张照片分支**

复用 photo-server 内部函数：
- `extract_exif()` — EXIF 提取
- `detect_faces()` — 人脸检测
- `match_face_to_person()` — 人物匹配
- `update_person_center()` — 更新中心嵌入
- `generate_l0_abstract()` — L0 摘要
- `sync_photo_to_kg()` — KG 同步
- `get_workspace_path()` — 工作区路径
- `build_photo_storage_path()` / `build_photo_file_name()` — 存储路径
- `handle_photo_conflict()` — 冲突处理
- `get_connection()` — 数据库连接

通过 `sys.path` 导入 `niu_photo_server` 模块。

- [ ] **Step 4: 运行测试确认通过**

- [ ] **Step 5: 提交**

---

### Task 3: 照片目录批量入库

**Files:**
- Modify: `scripts/ingest_unified.py`
- Modify: `tests/test_ingest_unified.py`

- [ ] **Step 1: 写失败测试 — 照片目录批量入库**

```python
class TestIngestPhotoBatch:
    def test_photo_directory_ingest(self):
        result = ingest(path="E:/tmp/2009.6.4西柏坡")
        assert result["status"] == "success"
        assert result["total"] > 0
        assert result["success"] > 0
        assert "results" in result
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现 ingest() — 目录照片分支**

逐张调用单张照片处理逻辑，汇总结果。

- [ ] **Step 4: 运行测试确认通过**

- [ ] **Step 5: 提交**

---

### Task 4: 单个文档入库 + L1 循环

**Files:**
- Modify: `scripts/ingest_unified.py`
- Modify: `tests/test_ingest_unified.py`

- [ ] **Step 1: 写失败测试 — 文档入库返回 need_l1**

```python
class TestIngestDocument:
    def test_document_returns_need_l1(self):
        result = ingest(path="docs/SYSTEM_MANUAL.md")
        assert result["status"] == "need_l1"
        assert "file_path" in result
        assert "content" in result

    def test_document_store_l1(self):
        # 先入库
        result1 = ingest(path="docs/SYSTEM_MANUAL.md")
        assert result1["status"] == "need_l1"
        # 送回 L1
        l1 = "系统手册|系统,配置,故障|完整的系统管理手册|SYSTEM_MANUAL|document|docs/SYSTEM_MANUAL.md"
        result2 = ingest(file_path=result1["file_path"], l1=l1)
        assert result2["status"] == "success"
```

- [ ] **Step 2: 运行测试确认失败**

- [ ] **Step 3: 实现 ingest() — 文档分支**

- 拷贝文件到文档目录
- 读取文件内容
- 如果有 l1 参数：存储 L1/L2 到向量库 + KG 同步
- 如果没有 l1 参数：返回 need_l1

- [ ] **Step 4: 运行测试确认通过**

- [ ] **Step 5: 提交**

---

### Task 5: 混合目录处理

**Files:**
- Modify: `scripts/ingest_unified.py`
- Modify: `tests/test_ingest_unified.py`

- [ ] **Step 1: 写失败测试 — 混合目录**

```python
class TestIngestMixed:
    def test_mixed_directory(self, tmp_path):
        # 创建混合目录：1张照片 + 1个文档
        import shutil
        shutil.copy("E:/tmp/2009.6.4西柏坡/DSC_3272.jpg", tmp_path / "photo.jpg")
        shutil.copy("docs/SYSTEM_MANUAL.md", tmp_path / "manual.md")
        result = ingest(path=str(tmp_path))
        assert result["status"] == "success"
        assert result["photos"]["total"] >= 1
        assert result["documents"]["need_l1"] >= 1
```

- [ ] **Step 2-5: 同上模式**

---

### Task 6: 错误处理 + 边界情况

**Files:**
- Modify: `scripts/ingest_unified.py`
- Modify: `tests/test_ingest_unified.py`

- [ ] **Step 1: 写失败测试**

```python
class TestIngestErrors:
    def test_nonexistent_path(self):
        result = ingest(path="E:/tmp/nonexistent.jpg")
        assert result["status"] == "error"
        assert result["error_code"] == "FILE_NOT_FOUND"

    def test_empty_directory(self, tmp_path):
        result = ingest(path=str(tmp_path))
        assert result["status"] == "error"
        assert result["error_code"] == "EMPTY_DIRECTORY"
```

- [ ] **Step 2-5: 同上模式**

---

### Task 7: 替换 photo-server 中的旧工具

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` — 添加 ingest TOOL_SCHEMA，删除旧 4 个工具的 schema
- Modify: `config/mcp-servers.yaml` — 更新 photo-server 工具 visibility

- [ ] **Step 1: 在 photo-server 中注册新 ingest 工具**

将 `scripts/ingest_unified.py` 的核心逻辑迁移到 `niu_photo_server/__init__.py`，注册为 MCP 工具。

- [ ] **Step 2: 删除旧工具的 TOOL_SCHEMAS**

删除 ingest_photo、ingest_photos、ingest_document、ingest_documents、store_document_l1、store_documents_l1 的 schema。

- [ ] **Step 3: 更新 mcp-servers.yaml**

```yaml
photo-server:
  tools:
    ingest: {visibility: dynamic}  # 子 Agent 调用
    name_person: {visibility: dynamic}
    # ... 其余不变
```

- [ ] **Step 4: 更新子 Agent file-processor.md**

修改子 Agent 提示词，改为调用 `ingest` 工具。

- [ ] **Step 5: 重新初始化向量库 + 端到端测试**

- [ ] **Step 6: 提交**
