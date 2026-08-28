#!/usr/bin/env python3
"""统一入库工具测试（scripts/ingest_unified.py 已废弃——测试仅守护路由/分类契约）"""

import sys
from pathlib import Path
from unittest.mock import patch

# 添加 scripts 目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from ingest_unified import ContentType, PathType, classify_path, ingest


class TestClassifyPath:
    def test_single_photo(self, tmp_path):
        photo = tmp_path / "DSC_3272.jpg"
        photo.write_bytes(b"fake-jpeg")
        result = classify_path(photo)
        assert result.path_type == PathType.FILE
        assert result.content_type == ContentType.PHOTO

    def test_single_document(self):
        result = classify_path(Path("docs/SYSTEM_MANUAL.md"))
        assert result.path_type == PathType.FILE
        assert result.content_type == ContentType.DOCUMENT

    def test_photo_directory(self, tmp_path):
        (tmp_path / "img1.jpg").touch()
        (tmp_path / "img2.png").touch()
        result = classify_path(tmp_path)
        assert result.path_type == PathType.DIRECTORY
        assert result.content_type == ContentType.PHOTO

    def test_nonexistent(self):
        result = classify_path(Path("/nonexistent/definitely_missing"))
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
    def test_single_photo_ingest(self, tmp_path):
        """ingest 路由到 _ingest_single_photo（子管道 mock，守护路由契约）。"""
        photo = tmp_path / "DSC_3272.jpg"
        photo.write_bytes(b"fake-jpeg")
        fake_result = {
            "status": "success",
            "photo_id": "p1",
            "file_path": str(photo),
            "detected_persons": [],
            "abstract": "测试摘要",
            "exif": {},
            "lightrag_sync": {"status": "ok"},
        }
        with patch("ingest_unified._ingest_single_photo", return_value=fake_result) as mock_photo:
            result = ingest(path=str(photo))
        mock_photo.assert_called_once_with(str(photo), None, "copy")
        assert result["status"] == "success", f"Expected success, got: {result}"
        assert "photo_id" in result
        assert "detected_persons" in result
        assert "file_path" in result
        assert Path(result["file_path"]).exists()


class TestIngestPhotoBatch:
    def test_photo_directory_ingest(self, tmp_path):
        """照片目录路由到 _ingest_photo_directory（子管道 mock，守护路由契约）。"""
        (tmp_path / "img1.jpg").touch()
        (tmp_path / "img2.jpg").touch()
        fake_result = {"status": "success", "source_path": str(tmp_path), "total": 2, "success": 2, "failed": 0, "results": []}
        with patch("ingest_unified._ingest_photo_directory", return_value=fake_result) as mock_dir:
            result = ingest(path=str(tmp_path))
        mock_dir.assert_called_once()
        assert result["status"] == "success", f"Expected success, got: {result}"
        assert result["total"] > 0
        assert result["success"] > 0


class TestIngestDocument:
    def test_document_returns_need_l1(self, tmp_path):
        """文档入库子管道 mock，守护 need_l1 契约。"""
        import uuid as _uuid
        doc = tmp_path / f"test_doc_{_uuid.uuid4().hex[:8]}.md"
        doc.write_text(f"# 测试文档 {_uuid.uuid4().hex[:8]}\n\n这是一个测试文档的内容。", encoding="utf-8")
        fake_result = {
            "status": "need_l1",
            "action": "created",
            "file_path": str(doc),
            "original_path": str(doc),
            "category": "其他",
            "content": doc.read_text(encoding="utf-8"),
            "hint": "请生成 L1 摘要",
        }
        with patch("ingest_unified._ingest_single_document", return_value=fake_result) as mock_doc:
            result = ingest(path=str(doc))
        mock_doc.assert_called_once()
        assert result["status"] == "need_l1", f"Expected need_l1, got: {result}"
        assert "file_path" in result
        assert "content" in result

    def test_document_store_l1(self, tmp_path):
        """L1 回传模式：ingest(file_path=..., l1=...) 走 photo-server ingest_document。"""
        import uuid as _uuid
        doc = tmp_path / f"test_doc2_{_uuid.uuid4().hex[:8]}.md"
        doc.write_text(f"# 测试文档2 {_uuid.uuid4().hex[:8]}\n\n这是另一个测试文档。", encoding="utf-8")
        with patch("ingest_unified._get_photo_server") as mock_ps:
            mock_ps.return_value.ingest_document.return_value = {"status": "success"}
            result = ingest(path="", file_path=str(doc), l1="测试文档|测试,文档|摘要|实体|document|file.md")
        assert result["status"] == "success", f"Expected success, got: {result}"
        assert result["file_path"] == str(doc)


class TestIngestErrors:
    def test_nonexistent_path(self):
        result = ingest(path="/nonexistent/definitely_missing.jpg")
        assert result["status"] == "error"
        assert result["error_code"] == "FILE_NOT_FOUND"

    def test_empty_directory(self, tmp_path):
        result = ingest(path=str(tmp_path))
        assert result["status"] == "error"
        assert result["error_code"] == "EMPTY_DIRECTORY"


class TestIngestMixed:
    def test_mixed_directory(self, tmp_path):
        """混合目录：photos/documents 两部分子管道 mock，守护聚合形状契约。"""
        (tmp_path / "photo.jpg").write_bytes(b"fake-jpeg")
        doc = tmp_path / "report.md"
        doc.write_text("# 混合目录测试报告\n\n测试内容。", encoding="utf-8")

        photo_result = {"status": "success", "source_path": str(tmp_path), "total": 1, "success": 1, "failed": 0, "results": []}
        doc_result = {"status": "need_l1", "total": 1, "need_l1": 1, "files": [], "hint": "请为每个文件生成 L1 摘要"}
        with patch("ingest_unified._ingest_photo_directory", return_value=photo_result), \
             patch("ingest_unified._ingest_document_directory", return_value=doc_result):
            result = ingest(path=str(tmp_path))
        assert result["status"] in ("success", "need_l1"), f"Unexpected: {result}"
        assert result["photos"]["total"] >= 1
        assert result["documents"]["total"] >= 1
