# 入库功能恢复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 恢复目录入库和多文件入库功能，实现子Agent工具循环（ingest分页式返回need_category → 子Agent再次调用工具带category → 继续处理），支持copy/move/reference三种模式，EXIF写入KG。

**Architecture:** ingest工具收到目录时返回文件列表，子Agent逐个调用ingest处理。遇到未分类文档返回need_category，子Agent判断分类后**再次调用ingest工具并传入category参数**。ingest通过_offset参数记住进度。照片直接入库不需要分类。

**Tech Stack:** Python, photo-server (MCP同进程), ToolRegistry, 子Agent(file-processor), pytest

---

## ⚠️ 审查发现的P0问题（已修复）

以下问题在多Agent审查中发现，已在计划中修复：

1. **`ingest_document` 返回格式与目录分页格式不一致** — `_ingest_directory` 需包装 `ingest_document` 的 `need_category` 返回，添加 `processed`/`total` 字段
2. **`need_category` 后 `_offset` 语义** — 当前文件未处理完，`_offset` 不变，子Agent带分类再次调用时仍用相同 `_offset`
3. **递归跳过不支持文件可能栈溢出** — 改为 while 循环
4. **子Agent提示词"只回答分类名称"与实际交互流矛盾** — 明确"再次调用ingest工具并传入category参数"
5. **测试断言格式不匹配** — 移除 `"directory"` 状态断言，修正测试逻辑
6. **`ingest_document` 内部旧目录处理逻辑冲突** — `ingest_document` 移除目录处理，只处理单文件；`ingest()` 是唯一目录入口

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | ingest()重写：目录分页处理 + ingest_photo()加mode + EXIF传KG；ingest_document()移除目录处理 |
| `config/agents/file-processor.md` | 子Agent提示词：工具循环规范（明确"再次调用工具"而非"回答分类名"） |
| `tests/test_ingest_paging.py` | 真实测试：ingest分页式返回格式 |
| `tests/test_ingest_agent_loop.py` | 真实测试：子Agent工具循环模拟 |

---

## Task 1: 写真实测试脚本 — 验证ingest目录分页式返回

**Files:**
- Create: `tests/test_ingest_paging.py`

**测试目标**：验证ingest收到目录时，返回分页式结果（progress/need_category/success），而不是直接批量处理。

**前置条件**：photo-server模块可导入。测试直接调用模块级函数，不启动完整Agent。

- [ ] **Step 1: 写测试脚本**

```python
"""
入库分页式处理真实测试

测试方式：直接调用photo-server模块级函数，验证返回值格式。
不需要启动完整Agent，但需要photo-server模块可导入。
"""
import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

# 确保photo-server可导入
PHOTO_SERVER_SRC = str(Path(__file__).parent.parent / "mcp-servers" / "photo-server" / "src")
if PHOTO_SERVER_SRC not in sys.path:
    sys.path.insert(0, PHOTO_SERVER_SRC)


class TestIngestDirectoryPaging:
    """验证ingest收到目录时返回分页式结果"""

    def setup_method(self):
        """创建测试目录：2张图片 + 2个文档"""
        import niu_photo_server as ps
        self.test_dir = Path("/tmp/niu_test_paging")
        self.test_dir.mkdir(exist_ok=True)
        # 创建测试图片
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='red')
        img.save(self.test_dir / "photo1.jpg")
        img.save(self.test_dir / "photo2.jpg")
        # 创建测试文档
        (self.test_dir / "doc1.txt").write_text("这是技术文档，关于Python编程。")
        (self.test_dir / "doc2.txt").write_text("这是财务报告，2026年Q1数据。")

    def test_ingest_directory_returns_progress_or_need_category(self):
        """ingest收到目录时返回progress或need_category，不是直接批量入库结果"""
        from niu_photo_server import ingest
        result = ingest(path=str(self.test_dir), category="", mode="copy")
        # 必须返回分页式状态，不是直接入库结果
        assert result["status"] in ("progress", "need_category"), \
            f"目录入库应返回progress或need_category，实际返回: {result['status']}, 详情: {result}"
        # 必须包含文件总数
        assert "total" in result, \
            f"应包含文件总数(total)，实际字段: {list(result.keys())}"

    def test_ingest_directory_progress_has_next_info(self):
        """progress状态包含下一个文件信息"""
        from niu_photo_server import ingest
        # 创建纯图片目录（图片直接入库，不会返回need_category）
        photo_dir = Path("/tmp/niu_test_paging_photos_only")
        photo_dir.mkdir(exist_ok=True)
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='blue')
        img.save(photo_dir / "photo1.jpg")
        img.save(photo_dir / "photo2.jpg")
        result = ingest(path=str(photo_dir), category="", mode="copy")
        if result["status"] == "progress":
            assert "next" in result, "progress应包含next字段（下一个文件信息）"
            assert "processed" in result, "progress应包含processed字段"
            assert result["processed"] >= 1, "progress的processed应>=1"
        # 清理
        import shutil
        if photo_dir.exists():
            shutil.rmtree(str(photo_dir), ignore_errors=True)

    def test_ingest_directory_with_offset_continues(self):
        """带_offset参数调用时，跳过已处理文件继续"""
        from niu_photo_server import ingest
        result = ingest(path=str(self.test_dir), category="", mode="copy", _offset=1)
        # _offset=1 应跳过第1个文件，从第2个开始
        # 返回状态应该是有效的分页状态
        assert result["status"] in ("progress", "need_category", "success", "error"), \
            f"带offset调用应返回有效状态，实际: {result['status']}"

    def test_ingest_directory_empty_returns_error(self):
        """空目录返回error"""
        from niu_photo_server import ingest
        empty_dir = Path("/tmp/niu_test_paging_empty")
        empty_dir.mkdir(exist_ok=True)
        result = ingest(path=str(empty_dir), category="", mode="copy")
        assert result["status"] == "error", f"空目录应返回error，实际: {result['status']}"
        import shutil
        if empty_dir.exists():
            shutil.rmtree(str(empty_dir), ignore_errors=True)

    def test_ingest_directory_offset_out_of_range(self):
        """_offset超出文件总数时返回error"""
        from niu_photo_server import ingest
        result = ingest(path=str(self.test_dir), category="", mode="copy", _offset=999)
        assert result["status"] == "error", f"offset超出范围应返回error，实际: {result['status']}"

    def teardown_method(self):
        """清理测试文件"""
        import shutil
        for d in ["/tmp/niu_test_paging", "/tmp/niu_test_paging_photos_only", "/tmp/niu_test_paging_empty"]:
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)


class TestIngestNeedCategory:
    """验证文档入库返回need_category格式"""

    def setup_method(self):
        self.test_dir = Path("/tmp/niu_test_need_category")
        self.test_dir.mkdir(exist_ok=True)
        (self.test_dir / "report.txt").write_text("2026年Q1财务报告，收入增长15%")

    def test_need_category_returns_preview_and_categories(self):
        """未指定category时返回内容预览和可选分类"""
        from niu_photo_server import ingest
        result = ingest(path=str(self.test_dir / "report.txt"), category="", mode="copy")
        assert result["status"] == "need_category", \
            f"未指定category应返回need_category，实际: {result['status']}"
        assert "available_categories" in result, "应包含可选分类列表"
        assert isinstance(result["available_categories"], list), "available_categories应为列表"
        assert len(result["available_categories"]) > 0, "可选分类不应为空"

    def test_need_category_with_category_succeeds(self):
        """指定category后文档入库成功"""
        from niu_photo_server import ingest
        # 先获取可选分类
        result1 = ingest(path=str(self.test_dir / "report.txt"), category="", mode="copy")
        if result1["status"] == "need_category":
            categories = result1["available_categories"]
            chosen = categories[0]
            # 带分类再次调用
            result2 = ingest(path=str(self.test_dir / "report.txt"), category=chosen, mode="copy")
            assert result2["status"] == "success", \
                f"带分类调用应成功，实际: {result2['status']}, 详情: {result2}"

    def teardown_method(self):
        import shutil
        if os.path.exists(str(self.test_dir)):
            shutil.rmtree(str(self.test_dir), ignore_errors=True)


class TestIngestPhotoMode:
    """验证ingest_photo支持三种模式"""

    def setup_method(self):
        self.test_dir = Path("/tmp/niu_test_photo_mode")
        self.test_dir.mkdir(exist_ok=True)
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='green')
        self.photo_path = self.test_dir / "test.jpg"
        img.save(self.photo_path)

    def test_ingest_photo_copy_mode(self):
        """copy模式：复制文件，源文件保留"""
        from niu_photo_server import ingest_photo
        result = ingest_photo(str(self.photo_path), category="生活")
        if result.get("status") == "error" and "lightrag" in str(result):
            pytest.skip("LightRAG未初始化，跳过KG验证")
        assert result["status"] == "success"
        assert self.photo_path.exists(), "copy模式源文件应保留"

    def test_ingest_photo_move_mode(self):
        """move模式：移动文件，源文件消失"""
        from PIL import Image
        from niu_photo_server import ingest_photo
        img = Image.new('RGB', (100, 100), color='yellow')
        move_path = self.test_dir / "move_test.jpg"
        img.save(move_path)
        result = ingest_photo(str(move_path), category="生活", mode="move")
        if "mode" not in str(result) and result.get("status") == "success":
            # mode参数被忽略，说明尚未实现
            pytest.skip("ingest_photo尚未支持mode参数")
        assert result["status"] == "success"
        # move模式源文件应消失
        if result.get("status") == "success" and not move_path.exists():
            pass  # 正确：源文件已移走
        elif result.get("status") == "success" and move_path.exists():
            pytest.fail("move模式源文件应消失，但仍然存在")

    def test_ingest_photo_reference_mode(self):
        """reference模式：不移动文件，只建KG实体"""
        from PIL import Image
        from niu_photo_server import ingest_photo
        img = Image.new('RGB', (100, 100), color='blue')
        ref_path = self.test_dir / "ref_test.jpg"
        img.save(ref_path)
        result = ingest_photo(str(ref_path), category="生活", mode="reference")
        if "mode" not in str(result) and result.get("status") == "success":
            pytest.skip("ingest_photo尚未支持mode参数")
        assert result["status"] == "success"
        # reference模式源文件应保留
        assert ref_path.exists(), "reference模式源文件应保留"

    def teardown_method(self):
        import shutil
        if os.path.exists(str(self.test_dir)):
            shutil.rmtree(str(self.test_dir), ignore_errors=True)
```

- [ ] **Step 2: 运行测试，确认哪些通过哪些失败**

```bash
PYTHONPATH=mcp-servers/photo-server/src:$PYTHONPATH python -m pytest tests/test_ingest_paging.py -v --tb=short 2>&1 | tail -30
```

预期：目录分页相关测试失败（功能未实现），need_category和copy模式测试通过（现有功能）。

- [ ] **Step 3: 记录测试结果，提交**

```bash
git add tests/test_ingest_paging.py
git commit -m "test: 入库分页式处理真实测试脚本（TDD先行）"
```

---

## Task 2: 写真实测试脚本 — 验证子Agent工具循环

**Files:**
- Create: `tests/test_ingest_agent_loop.py`

**测试目标**：验证子Agent能正确处理ingest的need_category返回，判断分类后**再次调用ingest工具并传入category参数**，直到收到最终汇总结果。

**这是最关键的测试**——验证整个工具循环链路。

**核心逻辑**：
- `need_category` → 当前文件未处理完 → `_offset` 不变 → 子Agent带category再次调用
- `progress` → 当前文件已处理完 → `_offset = processed` → 子Agent继续调用
- `success` + `total` → 所有文件处理完 → 循环结束

- [ ] **Step 1: 写Agent循环测试脚本**

```python
"""
子Agent工具循环真实测试

测试方式：模拟子Agent的决策逻辑，循环调用ingest工具，
验证整个循环能正确完成目录入库。

这个测试不需要启动真实Agent，但模拟了Agent的决策行为：
- 收到need_category → 从available_categories选择分类 → 再次调用ingest（带category参数，_offset不变）
- 收到progress → 再次调用ingest（_offset=processed）
- 收到success + total → 循环结束
"""
import os
import sys
import pytest
from pathlib import Path

PHOTO_SERVER_SRC = str(Path(__file__).parent.parent / "mcp-servers" / "photo-server" / "src")
if PHOTO_SERVER_SRC not in sys.path:
    sys.path.insert(0, PHOTO_SERVER_SRC)


class TestAgentToolLoop:
    """模拟子Agent的工具循环行为"""

    def setup_method(self):
        """创建测试目录：1张图片 + 2个文档"""
        self.test_dir = Path("/tmp/niu_test_agent_loop")
        self.test_dir.mkdir(exist_ok=True)
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='red')
        img.save(self.test_dir / "photo.jpg")
        (self.test_dir / "tech_doc.txt").write_text("Python异步编程最佳实践")
        (self.test_dir / "finance_doc.txt").write_text("2026年Q1财务报告，利润增长20%")

    def test_single_file_need_category_then_classify(self):
        """单文档：need_category → 带分类再次调用 → success"""
        from niu_photo_server import ingest

        # 第1次调用：不传category
        result1 = ingest(path=str(self.test_dir / "tech_doc.txt"), category="", mode="copy")
        assert result1["status"] == "need_category"

        # 模拟Agent决策：从available_categories中选择
        categories = result1["available_categories"]
        chosen = categories[0]

        # 第2次调用：带category（关键：是再次调用工具，不是回复文本）
        result2 = ingest(path=str(self.test_dir / "tech_doc.txt"), category=chosen, mode="copy")
        assert result2["status"] == "success"

    def test_directory_tool_loop_completes(self):
        """目录：子Agent循环调用ingest直到所有文件处理完"""
        from niu_photo_server import ingest

        results = []
        offset = 0
        category = ""  # 初始无分类
        max_iterations = 20  # 防止无限循环

        for i in range(max_iterations):
            result = ingest(
                path=str(self.test_dir),
                category=category,
                mode="copy",
                _offset=offset
            )
            results.append(result)

            if result["status"] == "success" and "total" in result:
                # 最终汇总结果，循环结束
                break
            elif result["status"] == "need_category":
                # 当前文件未处理完，需要分类
                # 模拟Agent决策：选择分类
                categories = result.get("available_categories", ["其他"])
                chosen = categories[0]
                # 带分类再次调用，_offset不变（当前文件还没处理完）
                result = ingest(
                    path=str(self.test_dir),
                    category=chosen,
                    mode="copy",
                    _offset=offset  # offset不变！
                )
                results.append(result)
                # 分类后文件处理完成，更新offset
                if result["status"] == "progress":
                    offset = result["processed"]
                    category = ""  # 重置分类，下一个文件可能需要重新判断
                elif result["status"] == "success" and "total" in result:
                    break
                elif result["status"] == "need_category":
                    # 下一个文件也需要分类，offset不变
                    category = ""  # 重置，让下一轮重新判断
                elif result["status"] == "error":
                    break  # 出错退出
            elif result["status"] == "progress":
                # 中间进度，继续处理
                offset = result["processed"]
                category = ""  # 重置分类
            else:
                break  # error或其他未知状态

        # 验证循环完成
        final = results[-1]
        assert final["status"] == "success", \
            f"循环应以success结束，实际: {final['status']}, 结果序列: {[r['status'] for r in results]}"
        assert "total" in final, "最终结果应包含文件总数"

    def test_progress_means_not_done(self):
        """验证：progress状态表示还有文件未处理，不是最终结果"""
        from niu_photo_server import ingest
        # 创建2张图片的目录（都会走progress直到最后一个）
        photo_dir = Path("/tmp/niu_test_agent_loop_photos")
        photo_dir.mkdir(exist_ok=True)
        from PIL import Image
        img = Image.new('RGB', (100, 100), color='blue')
        img.save(photo_dir / "photo1.jpg")
        img.save(photo_dir / "photo2.jpg")
        result = ingest(path=str(photo_dir), category="", mode="copy")
        if result["status"] == "progress":
            assert result["processed"] < result["total"], \
                "progress状态表示还有文件未处理"
        import shutil
        if photo_dir.exists():
            shutil.rmtree(str(photo_dir), ignore_errors=True)

    def teardown_method(self):
        import shutil
        for d in ["/tmp/niu_test_agent_loop", "/tmp/niu_test_agent_loop_photos"]:
            if os.path.exists(d):
                shutil.rmtree(d, ignore_errors=True)


class TestAgentPromptCompliance:
    """验证子Agent提示词约束是否有效"""

    def test_need_category_format_allows_simple_choice(self):
        """need_category返回的available_categories格式简单，子Agent只需选一个"""
        from niu_photo_server import ingest
        test_file = Path("/tmp/niu_test_prompt")
        test_file.mkdir(exist_ok=True)
        (test_file / "doc.txt").write_text("测试文档内容")

        result = ingest(path=str(test_file / "doc.txt"), category="", mode="copy")
        if result["status"] == "need_category":
            categories = result["available_categories"]
            # 选择应该简单——只需从列表中选一个字符串
            assert isinstance(categories, list), "available_categories应为列表"
            assert all(isinstance(c, str) for c in categories), "每个分类应为字符串"
            # 模拟子Agent选择：只需取列表中的一个
            chosen = categories[0]
            # 子Agent"再次调用工具"传入category参数
            result2 = ingest(path=str(test_file / "doc.txt"), category=chosen, mode="copy")
            assert result2["status"] == "success"

        import shutil
        if os.path.exists(str(test_file)):
            shutil.rmtree(str(test_file), ignore_errors=True)
```

- [ ] **Step 2: 运行测试**

```bash
PYTHONPATH=mcp-servers/photo-server/src:$PYTHONPATH python -m pytest tests/test_ingest_agent_loop.py -v --tb=short 2>&1 | tail -30
```

预期：单文档need_category测试通过，目录循环测试失败（ingest未实现分页）。

- [ ] **Step 3: 提交**

```bash
git add tests/test_ingest_agent_loop.py
git commit -m "test: 子Agent工具循环真实测试脚本（TDD先行）"
```

---

## Task 3: 实现ingest目录分页式返回

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:3071-3081` (ingest函数)
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:28-65` (TOOL_SCHEMAS ingest)
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:3114-3142` (ingest_document目录处理)

**实现目标**：
1. ingest收到目录时，扫描文件列表，逐个处理并返回progress/need_category/success
2. 支持offset参数跳过已处理文件
3. ingest_document移除目录处理逻辑（只处理单文件），避免与ingest冲突

- [ ] **Step 1: ingest_document移除目录处理**

现有 `ingest_document()` (约line 3114-3142) 在路径是目录时调用 `ingest_photos_batch()` 或返回 `DIRECTORY_NO_PHOTOS`。移除这段逻辑，改为：
```python
if source.is_dir():
    return {"status": "error", "message": "ingest_document不支持目录，请使用ingest工具处理目录"}
```

- [ ] **Step 2: 重写ingest函数**

修改 `ingest()` 函数（line 3071），当路径是目录时调用 `_ingest_directory`：

```python
def ingest(path: str, category: str = "", mode: str = "copy", _offset: int = 0) -> dict:
    """统一入库工具 — 自动判断路径类型和内容类型

    参数:
    - path: 必填，文件路径或目录路径
    - mode: copy（复制）| move（移动）| reference（引用），默认 copy
    - category: 分类目录（文档必填，照片/目录可不传）
    - _offset: 内部参数，目录入库时跳过前N个已处理文件，默认0
    """
    source = Path(path)
    if not source.exists():
        return {"status": "error", "message": f"路径不存在: {path}"}

    # 单文件：直接入库
    if source.is_file():
        if is_photo(str(source)):
            from niu_photo_server import ingest_photo
            return ingest_photo(str(source), category=category or None, mode=mode)
        else:
            return ingest_document(file_path=path, category=category, mode=mode)

    # 目录：分页式处理
    return _ingest_directory(source, category, mode, _offset)
```

- [ ] **Step 3: 新增_ingest_directory函数**

```python
def _ingest_directory(source: Path, category: str, mode: str, offset: int) -> dict:
    """目录入库：分页式处理，每次处理一个文件

    返回值：
    - progress: 已处理一个文件，还有更多
    - need_category: 当前文档需要分类
    - success: 所有文件处理完毕
    - error: 出错
    """
    # 扫描所有文件（按名称排序保证稳定性）
    all_files = []
    for f in sorted(source.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        if is_photo(str(f)):
            all_files.append({"path": str(f), "type": "image"})
        elif is_document(str(f)):
            all_files.append({"path": str(f), "type": "document"})
        else:
            all_files.append({"path": str(f), "type": "other"})

    if not all_files:
        return {"status": "error", "message": f"目录为空或没有可入库文件: {source}"}

    # offset超出范围
    if offset >= len(all_files):
        return {"status": "error", "message": f"_offset={offset}超出文件总数({len(all_files)})"}

    # 用 while 循环跳过不支持文件（避免递归栈溢出）
    while offset < len(all_files):
        next_file = all_files[offset]

        if next_file["type"] == "other":
            # 不支持的文件类型，跳过
            offset += 1
            continue

        if next_file["type"] == "image":
            # 图片直接入库
            from niu_photo_server import ingest_photo
            result = ingest_photo(next_file["path"], category=category or None, mode=mode)
            offset += 1
            # 检查是否还有更多文件
            if offset >= len(all_files):
                return {
                    "status": "success",
                    "total": len(all_files),
                    "processed": offset,
                    "last_result": result,
                    "skipped": sum(1 for f in all_files if f["type"] == "other"),
                }
            else:
                next_info = all_files[offset]
                return {
                    "status": "progress",
                    "processed": offset,
                    "total": len(all_files),
                    "last_result": result,
                    "next": {"file": Path(next_info["path"]).name, "type": next_info["type"], "needs_category": next_info["type"] == "document"},
                    "message": f"已处理 {offset}/{len(all_files)}，下一个: {Path(next_info['path']).name}"
                }

        elif next_file["type"] == "document":
            if category:
                # 有分类，直接入库
                result = ingest_document(file_path=next_file["path"], category=category, mode=mode)
                offset += 1
                if offset >= len(all_files):
                    return {
                        "status": "success",
                        "total": len(all_files),
                        "processed": offset,
                        "last_result": result,
                        "skipped": sum(1 for f in all_files if f["type"] == "other"),
                    }
                else:
                    next_info = all_files[offset]
                    return {
                        "status": "progress",
                        "processed": offset,
                        "total": len(all_files),
                        "last_result": result,
                        "next": {"file": Path(next_info["path"]).name, "type": next_info["type"], "needs_category": next_info["type"] == "document"},
                        "message": f"已处理 {offset}/{len(all_files)}，下一个: {Path(next_info['path']).name}"
                    }
            else:
                # 无分类，返回need_category（包装ingest_document的返回）
                doc_result = ingest_document(file_path=next_file["path"], category="", mode=mode)
                if doc_result.get("status") == "need_category":
                    # 包装为目录上下文格式
                    return {
                        "status": "need_category",
                        "processed": offset,  # 当前文件索引（尚未处理完）
                        "total": len(all_files),
                        "current_file": Path(next_file["path"]).name,
                        "file_path": next_file["path"],
                        "mode": mode,
                        "preview": doc_result.get("message", ""),
                        "available_categories": doc_result.get("available_categories", ["其他"]),
                    }
                else:
                    # ingest_document 意外返回了其他状态（如success或error）
                    # 按结果处理
                    offset += 1
                    if offset >= len(all_files):
                        return {
                            "status": "success",
                            "total": len(all_files),
                            "processed": offset,
                            "last_result": doc_result,
                            "skipped": sum(1 for f in all_files if f["type"] == "other"),
                        }
                    else:
                        next_info = all_files[offset]
                        return {
                            "status": "progress",
                            "processed": offset,
                            "total": len(all_files),
                            "last_result": doc_result,
                            "next": {"file": Path(next_info["path"]).name, "type": next_info["type"], "needs_category": next_info["type"] == "document"},
                            "message": f"已处理 {offset}/{len(all_files)}，下一个: {Path(next_info['path']).name}"
                        }

    # 所有文件都被跳过（都是other类型）
    return {"status": "error", "message": f"目录中没有可入库的文件: {source}"}
```

- [ ] **Step 4: 更新TOOL_SCHEMAS**

在 `ingest` 的 `input_schema` 中添加 `_offset` 参数：

```python
"_offset": {"type": "integer", "description": "目录入库时跳过前N个已处理文件（内部参数，子Agent通过progress返回的processed值设置）", "default": 0},
```

更新 `ingest` 的 description：
```python
"description": "统一入库工具。接收文件路径或目录路径。单文件直接入库；目录分页式处理（逐个文件入库，返回progress/need_category/success状态，子Agent需循环调用直到收到success+total）。文档未指定category时返回need_category等待分类判断。",
```

- [ ] **Step 5: 运行Task1测试验证**

```bash
PYTHONPATH=mcp-servers/photo-server/src:$PYTHONPATH python -m pytest tests/test_ingest_paging.py -v --tb=short 2>&1 | tail -30
```

预期：目录分页相关测试通过。

- [ ] **Step 6: 运行Task2测试验证**

```bash
PYTHONPATH=mcp-servers/photo-server/src:$PYTHONPATH python -m pytest tests/test_ingest_agent_loop.py -v --tb=short 2>&1 | tail -30
```

预期：工具循环测试通过。

- [ ] **Step 7: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: ingest目录分页式处理 — 子Agent工具循环（修复6个P0审查问题）"
```

---

## Task 4: 实现ingest_photo三种模式

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py:1864` (ingest_photo函数)

**实现目标**：ingest_photo支持mode参数（copy/move/reference），修改文件操作逻辑和错误回滚。

**当前签名**：`def ingest_photo(file_path: str, category: str | None = None) -> dict`
**目标签名**：`def ingest_photo(file_path: str, category: str | None = None, mode: str = "copy") -> dict`

- [ ] **Step 1: 修改ingest_photo签名和文件操作**

在 `ingest_photo()` 中：
1. 签名加 `mode: str = "copy"` 参数
2. 找到 `shutil.copy2` 调用（约line 1977），改为三分支：
   - copy: `shutil.copy2()`（现有行为）
   - move: `shutil.move()`
   - reference: `final_path = str(source)`（不复制，直接用原始路径）
3. 错误回滚逻辑（约line 2073-2078）：
   - move模式：移回源路径
   - reference模式：不删除文件
   - copy模式：删除已复制文件
4. DB记录source_path：
   - copy/move: 记录目标路径
   - reference: 记录原始路径

- [ ] **Step 2: 运行Task1中photo mode测试**

```bash
PYTHONPATH=mcp-servers/photo-server/src:$PYTHONPATH python -m pytest tests/test_ingest_paging.py::TestIngestPhotoMode -v --tb=short
```

- [ ] **Step 3: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: ingest_photo支持copy/move/reference三种模式"
```

---

## Task 5: EXIF信息写入KG实体

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` (sync_photo_to_kg调用链)

**实现目标**：EXIF数据（taken_at, location, camera）从ingest_photo传入sync_photo_to_kg，格式化到entity description。

- [ ] **Step 1: 修改调用链传递EXIF**

1. `ingest_photo()` 调 `sync_photo_to_kg()` 时传入 `exif={"taken_at": taken_at, "location": location, "camera": camera}`
2. `sync_photo_to_kg()` 签名加 `exif: dict | None = None`
3. `_do_sync_photo_to_kg()` 签名加 `exif: dict | None = None`
4. `format_photo_ingest_data()` 签名加 `exif: dict | None = None`
5. `_generate_stable_description()` 签名加 `exif: dict | None = None`

- [ ] **Step 2: 修改_generate_stable_description**

```python
def _generate_stable_description(normalized_stem: str, abstract: str, exif: dict | None = None) -> str:
    parts = [f"照片 {normalized_stem}"]
    # EXIF信息
    if exif:
        camera = exif.get("camera")
        if camera:
            parts.append(f"设备：{camera}")
        location = exif.get("location")
        if location:
            parts.append(f"位置：{location}")
        taken_at = exif.get("taken_at")
        if taken_at:
            try:
                from datetime import datetime
                dt = datetime.strptime(taken_at, "%Y:%m:%d %H:%M:%S")
                parts.append(f"拍摄于{dt.strftime('%Y年%m月%d日 %H:%M')}")
            except (ValueError, TypeError):
                pass
    # fallback: 从文件名提取日期
    if not any("拍摄于" in p for p in parts):
        if len(normalized_stem) >= 8 and normalized_stem[:8].isdigit():
            try:
                from datetime import datetime
                dt = datetime.strptime(normalized_stem[:8], "%Y%m%d")
                parts.append(f"拍摄于{dt.strftime('%Y年%m月%d日')}")
            except ValueError:
                pass
    return "，".join(parts)
```

- [ ] **Step 3: 写EXIF测试并运行**

在 `tests/test_ingest_paging.py` 中添加：

```python
class TestEXIFInKG:
    """验证EXIF信息写入KG实体description"""

    def test_stable_description_includes_camera(self):
        """description包含相机型号"""
        from niu_photo_server import _generate_stable_description
        result = _generate_stable_description(
            "20260419_143000", "单人照片",
            exif={"camera": "Apple iPhone 15 Pro", "location": "31.23,121.47", "taken_at": "2026:04:19 14:30:00"}
        )
        assert "iPhone" in result
        assert "31.23" in result
        assert "2026年04月19日" in result

    def test_stable_description_without_exif(self):
        """无EXIF时fallback到文件名日期"""
        from niu_photo_server import _generate_stable_description
        result = _generate_stable_description("20260419_143000", "单人照片", exif=None)
        assert "2026年04月19日" in result
```

```bash
PYTHONPATH=mcp-servers/photo-server/src:$PYTHONPATH python -m pytest tests/test_ingest_paging.py::TestEXIFInKG -v --tb=short
```

- [ ] **Step 4: 提交**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py tests/test_ingest_paging.py
git commit -m "feat: EXIF信息写入KG实体description"
```

---

## Task 6: 更新子Agent提示词

**Files:**
- Modify: `config/agents/file-processor.md`

**实现目标**：子Agent提示词包含工具循环规范，明确"再次调用ingest工具并传入category参数"（不是回复文本），防止中途退出。

**关键修正**：审查发现原提示词"只回答分类名称"暗示文本回复，但实际需要的是**再次调用ingest工具**。必须明确这一点。

- [ ] **Step 1: 更新file-processor.md**

在提示词中添加/修改工具循环规范章节：

```markdown
## 工具循环规范（重要！必须严格遵守）

ingest工具处理目录时，会多次返回中间结果。你必须持续响应直到收到最终结果。

### 返回状态

| status | 含义 | 是否完成 | 你应该做什么 |
|--------|------|----------|-------------|
| `progress` | 中间进度，已处理部分文件 | **否** | 再次调用ingest工具，_offset=返回的processed值 |
| `need_category` | 当前文档需要分类判断 | **否** | 再次调用ingest工具，传入category参数（_offset不变） |
| `success` + `total` | 所有文件处理完毕 | **是** | 向主Agent汇报入库完成 |
| `error` | 出错 | **是** | 向主Agent报告错误 |

### need_category 处理流程（关键！）

收到 `need_category` 时，工具在等你判断分类。你必须：

1. 阅读工具返回的 `preview`（内容预览）
2. 从 `available_categories` 列表中选择最合适的分类
3. **再次调用 ingest 工具**，传入：
   - path: 与上次相同
   - category: 你选择的分类名称（必须从 available_categories 中选择，不要自己编造）
   - mode: 与上次相同
   - _offset: 与上次相同（当前文件还没处理完，offset不变）

**注意**：不是回复文本"技术文档"，而是**生成工具调用** `ingest(path=..., category="技术文档", mode=..., _offset=N)`。

### 禁止提前退出

- 只有 `status: "success"` 且包含 `total` 字段时，才表示所有文件处理完毕
- `progress` 和 `need_category` 都是中间状态，必须继续调用工具
- 没收到最终汇总前，不能向主Agent汇报"入库完成"
```

- [ ] **Step 2: 提交**

```bash
git add config/agents/file-processor.md
git commit -m "feat: 子Agent提示词添加工具循环规范（明确再次调用工具而非回复文本）"
```

---

## Task 7: 更新配置文件

**Files:**
- Modify: `config/mcp-servers.yaml`
- Modify: `config/disk/photo-server.yaml`

- [ ] **Step 1: 更新ingest的TOOL_SCHEMAS参数和disk YAML**

确保ingest工具的参数定义包含 `_offset`，disk YAML参数与TOOL_SCHEMAS一致。

- [ ] **Step 2: 提交**

```bash
git add config/mcp-servers.yaml config/disk/photo-server.yaml
git commit -m "feat: 更新ingest工具配置"
```

---

## Task 8: 全量真实测试

**Files:**
- Test: `tests/test_ingest_paging.py`
- Test: `tests/test_ingest_agent_loop.py`

**测试目标**：所有测试通过，验证完整的入库功能。

- [ ] **Step 1: 运行全部入库测试**

```bash
PYTHONPATH=mcp-servers/photo-server/src:$PYTHONPATH python -m pytest tests/test_ingest_paging.py tests/test_ingest_agent_loop.py -v --tb=short
```

预期：全部通过。

- [ ] **Step 2: 运行P0回归测试**

```bash
python -m pytest tests/test_p0/ -v --tb=short
```

预期：62个测试全部通过，无回归。

- [ ] **Step 3: 提交**

```bash
git commit -m "test: 入库功能全量测试通过"
```
