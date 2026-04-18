#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一入库工具测试"""

import sys
from pathlib import Path

# 添加 scripts 目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from ingest_unified import classify_path, ingest, PathType, ContentType


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

    def test_empty_directory(self, tmp_path):
        result = classify_path(tmp_path)
        assert result.path_type == PathType.DIRECTORY
        assert result.content_type == ContentType.EMPTY

    def test_doc_directory(self, tmp_path):
        (tmp_path / "report.pdf").touch()
        (tmp_path / "notes.docx").touch()
        result = classify_path(tmp_path)
        assert result.path_type == PathType.DIRECTORY
        assert result.content_type == ContentType.DOCUMENT


class TestIngestPhoto:
    def test_single_photo_ingest(self):
        result = ingest(path="E:/tmp/2009.6.4西柏坡/DSC_3272.jpg")
        assert result["status"] == "success", f"Expected success, got: {result}"
        assert "photo_id" in result
        assert "detected_persons" in result
        assert "file_path" in result
        assert Path(result["file_path"]).exists()


class TestIngestPhotoBatch:
    def test_photo_directory_ingest(self):
        result = ingest(path="E:/tmp/2009.6.4西柏坡")
        assert result["status"] == "success", f"Expected success, got: {result}"
        assert result["total"] > 0
        assert result["success"] > 0


class TestIngestDocument:
    def test_document_returns_need_l1(self, tmp_path):
        # 创建一个新文档，用 uuid 避免和已有文档冲突
        import uuid as _uuid
        doc = tmp_path / f"test_doc_{_uuid.uuid4().hex[:8]}.md"
        doc.write_text(f"# 测试文档 {_uuid.uuid4().hex[:8]}\n\n这是一个测试文档的内容。", encoding="utf-8")
        result = ingest(path=str(doc))
        assert result["status"] == "need_l1", f"Expected need_l1, got: {result}"
        assert "file_path" in result
        assert "content" in result

    def test_document_store_l1(self, tmp_path):
        import uuid as _uuid
        # 创建一个新文档
        doc = tmp_path / f"test_doc2_{_uuid.uuid4().hex[:8]}.md"
        doc.write_text(f"# 测试文档2 {_uuid.uuid4().hex[:8]}\n\n这是另一个测试文档。", encoding="utf-8")
        # 先入库
        result1 = ingest(path=str(doc))
        assert result1["status"] == "need_l1", f"Expected need_l1, got: {result1}"
        # 送回 L1
        l1 = "测试文档|测试,文档|测试文档摘要|test_doc2|document|test_doc2.md"
        result2 = ingest(path="", file_path=result1["file_path"], l1=l1)
        assert result2["status"] == "success", f"Expected success, got: {result2}"


class TestIngestErrors:
    def test_nonexistent_path(self):
        result = ingest(path="E:/tmp/nonexistent.jpg")
        assert result["status"] == "error"
        assert result["error_code"] == "FILE_NOT_FOUND"

    def test_empty_directory(self, tmp_path):
        result = ingest(path=str(tmp_path))
        assert result["status"] == "error"
        assert result["error_code"] == "EMPTY_DIRECTORY"


class TestIngestMixed:
    def test_mixed_directory(self, tmp_path):
        """混合目录：照片走照片流程，文档走文档流程"""
        import shutil
        import uuid as _uuid
        # 拷贝一张真实照片
        photo_src = Path("E:/tmp/2009.6.4西柏坡/DSC_3272.jpg")
        if photo_src.exists():
            shutil.copy2(str(photo_src), str(tmp_path / "photo.jpg"))
        # 创建一个新文档
        doc = tmp_path / f"report_{_uuid.uuid4().hex[:8]}.md"
        doc.write_text("# 混合目录测试报告\n\n测试内容。", encoding="utf-8")
        result = ingest(path=str(tmp_path))
        assert result["status"] in ("success", "need_l1"), f"Unexpected: {result}"
        assert result["photos"]["total"] >= 1
        assert result["documents"]["total"] >= 1
