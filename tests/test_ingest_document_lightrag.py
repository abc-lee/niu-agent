"""Tests for ingest_document modifications:
1. Returns status: "success" without need_l1
2. Calls lightrag-server/lightrag_insert_file with file path
3. No calls to vector-store or kg-server tools
4. Auto-detects file type (directory/photo/document)
5. File path passed to LightRAG, not text content
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers" / "photo-server" / "src"))


@pytest.fixture
def mock_registry():
    """Mock the tool registry so lightrag_insert is callable without real server.

    ingest_document imports get_registry locally from agent.tool_registry,
    so we must patch that import path.
    """
    mock_insert = MagicMock(return_value={"status": "success", "chunks": 5})
    mock_reg = MagicMock()
    mock_reg.get.return_value = mock_insert

    with patch("agent.tool_registry.get_registry", return_value=mock_reg):
        yield mock_reg, mock_insert


@pytest.fixture
def mock_preferences(tmp_path):
    """Provide a minimal workspace path for document storage."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch("niu_photo_server.get_workspace_path", return_value=workspace):
        with patch("niu_photo_server.build_storage_path", return_value="documents/2026"):
            yield workspace


# ---------------------------------------------------------------------------
# 1. ingest_document returns status "success" without need_l1
# ---------------------------------------------------------------------------


class TestNoNeedL1:
    """Verify that need_l1 is never returned."""

    def test_document_returns_success_not_need_l1(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        doc = tmp_path / "source" / "report.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Annual report content here.", encoding="utf-8")

        result = ingest_document(str(doc), category="报告", mode="copy")

        assert result["status"] == "success"
        assert "need_l1" not in result
        assert "vector_db" not in result
        assert "knowledge_graph" not in result

    def test_skipped_file_returns_success_no_need_l1(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        doc = tmp_path / "source" / "existing.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Already ingested.", encoding="utf-8")

        # Pre-create the target to trigger "skipped" action
        with patch("niu_photo_server.handle_conflict", return_value=(str(doc), "skipped")):
            result = ingest_document(str(doc), category="其他", mode="copy")

        assert result["status"] == "success"
        assert result.get("action") == "skipped"
        assert "need_l1" not in result


# ---------------------------------------------------------------------------
# 2. ingest_document calls lightrag-server/lightrag_insert_file with file path
# ---------------------------------------------------------------------------


class TestLightragInsertCall:
    """Verify that lightrag_insert_file is called with file path."""

    def test_calls_lightrag_insert_file_with_file_path(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        content = "This is the full document content. " * 100  # ~3900 chars
        doc = tmp_path / "source" / "full_doc.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(content, encoding="utf-8")

        ingest_document(str(doc), category="其他", mode="copy")

        # lightrag_insert_file should have been called (not lightrag_insert)
        mock_reg.get.assert_called_with("lightrag-server/lightrag_insert_file")
        mock_insert.assert_called_once()

        call_kwargs = mock_insert.call_args.kwargs
        # Verify file_path is passed (not content)
        assert "file_path" in call_kwargs, "lightrag_insert_file should receive file_path"
        assert "content" not in call_kwargs, "lightrag_insert_file should NOT receive content"

    def test_lightrag_insert_file_receives_file_path(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        doc = tmp_path / "source" / "path_test.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Some content.", encoding="utf-8")

        ingest_document(str(doc), category="其他", mode="copy")

        mock_insert.assert_called_once()
        call_kwargs = mock_insert.call_args.kwargs
        assert "file_path" in call_kwargs, "lightrag_insert_file should receive file_path"

    def test_lightrag_field_in_result(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        doc = tmp_path / "source" / "lightrag_field.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Check lightrag field.", encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        assert "lightrag" in result
        assert result["lightrag"] in ("inserted", "skipped")

    def test_lightrag_insert_file_always_called(self, mock_registry, mock_preferences, tmp_path):
        """KG 支持扩展名的文件总是调用 lightrag_insert_file（内容不参与判定）。

        现役契约：check_kg_supported 按扩展名白名单判定，白名单内文件
        （即使内容是二进制噪音）也调用 lightrag_insert_file，由 LightRAG 自行解析。
        """
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        # .txt 在白名单内，写入二进制噪音内容验证调用与内容无关
        doc = tmp_path / "source" / "binary.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"\x00\x01\x02\x03\xff\xfe")

        ingest_document(str(doc), category="其他", mode="copy")

        # insert_file should be called even for binary content (supported extension)
        mock_reg.get.assert_called_with("lightrag-server/lightrag_insert_file")
        mock_insert.assert_called_once()


# ---------------------------------------------------------------------------
# 3. No calls to vector-store or kg-server tools
# ---------------------------------------------------------------------------


class TestNoVectorStoreOrKgServer:
    """Verify that vector-store and kg-server are never called."""

    def test_no_vector_store_calls(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        doc = tmp_path / "source" / "no_vs.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Content for no vector-store test.", encoding="utf-8")

        ingest_document(str(doc), category="其他", mode="copy")

        # Check that registry.get was never called with vector-store or kg-server
        mock_reg, _ = mock_registry
        for call in mock_reg.get.call_args_list:
            tool_name = call[0][0] if call[0] else call.kwargs.get("name", "")
            assert not tool_name.startswith("vector-store/"), \
                f"vector-store tool was called: {tool_name}"
            assert not tool_name.startswith("kg-server/"), \
                f"kg-server tool was called: {tool_name}"

    def test_no_store_document_l1_called(self, mock_registry, mock_preferences, tmp_path):
        """Ensure ingest_document does NOT call lightrag_insert (old tool) internally."""
        from niu_photo_server import ingest_document

        doc = tmp_path / "source" / "no_l1.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Content for no L1 test.", encoding="utf-8")

        ingest_document(str(doc), category="其他", mode="copy")
        # Verify the old lightrag_insert tool was NOT called
        mock_reg, mock_insert = mock_registry
        for call in mock_reg.get.call_args_list:
            tool_name = call[0][0] if call[0] else ""
            assert tool_name != "lightrag-server/lightrag_insert", \
                "Old lightrag_insert should not be called"


# ---------------------------------------------------------------------------
# 4. Auto-detect file type (directory/photo/document)
# ---------------------------------------------------------------------------


class TestFileTypeDetection:
    """Verify auto-detection of file type."""

    def test_directory_returns_is_directory_error(self, mock_registry, mock_preferences, tmp_path):
        """目录不再批量转发——现役契约：目录应由有状态 ingest() 处理，ingest_document 直接拒绝。

        旧 ingest_photos_batch 已随入库 API 重构删除，目录处理迁移到
        ingest(path, action="start") 状态会话。
        """
        from niu_photo_server import ingest_document

        photo_dir = tmp_path / "photo_dir"
        photo_dir.mkdir()
        (photo_dir / "img1.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (photo_dir / "img2.png").write_bytes(b"\x89PNG")

        result = ingest_document(str(photo_dir), category="其他", mode="copy")
        assert result["status"] == "error"
        assert result["error_code"] == "IS_DIRECTORY"

    def test_directory_without_photos_returns_error(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        empty_dir = tmp_path / "empty_dir"
        empty_dir.mkdir()
        (empty_dir / "notes.txt").write_text("Just a text file.")

        result = ingest_document(str(empty_dir), category="其他", mode="copy")
        assert result["status"] == "error"
        assert result["error_code"] == "IS_DIRECTORY"

    def test_photo_file_delegates_to_ingest_photo(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0fake_jpg_data")

        with patch("niu_photo_server.ingest_photo") as mock_photo:
            mock_photo.return_value = {"status": "success", "action": "created"}
            ingest_document(str(photo), category="其他", mode="copy")
            # 现役契约：ingest_photo(path, category=..., mode=...) 关键字透传
            mock_photo.assert_called_once_with(str(photo), category="其他", mode="copy")

    def test_document_file_is_processed_as_document(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        doc = tmp_path / "source" / "notes.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Regular document content.", encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        # Should be processed as document (has content_length and lightrag field)
        assert result["status"] == "success"
        assert "content_length" in result
        assert "lightrag" in result


# ---------------------------------------------------------------------------
# 5. File path passed to LightRAG (no truncation — LightRAG reads the file)
# ---------------------------------------------------------------------------


class TestNoTruncation:
    """Verify that file_path is passed to LightRAG (no content truncation)."""

    def test_file_path_passed_not_content(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        # Create content longer than the old 10000-char truncation limit
        long_content = "X" * 15000
        doc = tmp_path / "source" / "long_doc.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(long_content, encoding="utf-8")

        ingest_document(str(doc), category="其他", mode="copy")

        mock_insert.assert_called_once()
        call_kwargs = mock_insert.call_args.kwargs
        # file_path should be passed, NOT content
        assert "file_path" in call_kwargs
        assert "content" not in call_kwargs

    def test_content_length_reflects_file_size(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        content = "Hello world! " * 500  # ~6500 chars
        doc = tmp_path / "source" / "size_check.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(content, encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        # content_length is now file size in bytes, not text char count
        assert result["content_length"] == doc.stat().st_size


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case handling."""

    def test_file_not_found(self, mock_registry, mock_preferences):
        from niu_photo_server import ingest_document

        result = ingest_document("/nonexistent/path/file.txt", category="其他")
        assert result["status"] == "error"
        assert result["error_code"] == "FILE_NOT_FOUND"

    def test_lightrag_failure_does_not_block_ingest(self, mock_registry, mock_preferences, tmp_path):
        """LightRAG insertion failure should not cause the whole ingest to fail."""
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry
        mock_insert.side_effect = Exception("LightRAG server is down")

        doc = tmp_path / "source" / "resilient.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Content that should still be ingested.", encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        # File should still be successfully ingested
        assert result["status"] == "success"
        assert result["lightrag"] == "skipped"  # LightRAG failed but file was copied
