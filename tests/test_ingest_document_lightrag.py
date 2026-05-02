"""Tests for ingest_document modifications:
1. Returns status: "success" without need_l1
2. Calls lightrag-server/lightrag_insert with full text content
3. No calls to vector-store or kg-server tools
4. Auto-detects file type (directory/photo/document)
5. Full-text to ainsert, no truncation for LightRAG
"""

import os
import sys
import textwrap
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
# 2. ingest_document calls lightrag-server/lightrag_insert with full text
# ---------------------------------------------------------------------------


class TestLightragInsertCall:
    """Verify that lightrag_insert is called with full content."""

    def test_calls_lightrag_insert_with_full_content(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        content = "This is the full document content. " * 100  # ~3900 chars
        doc = tmp_path / "source" / "full_doc.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(content, encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        # lightrag_insert should have been called
        mock_reg.get.assert_called_with("lightrag-server/lightrag_insert")
        mock_insert.assert_called_once()

        call_kwargs = mock_insert.call_args
        # Verify content is passed (not truncated)
        passed_content = call_kwargs.kwargs.get("content") or call_kwargs[1].get("content")
        if passed_content is None and call_kwargs[0]:
            passed_content = call_kwargs[0][0]
        assert passed_content is not None
        assert len(passed_content) == len(content), "Content should not be truncated for LightRAG"

    def test_lightrag_insert_receives_file_path(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        doc = tmp_path / "source" / "path_test.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Some content.", encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        mock_insert.assert_called_once()
        call_kwargs = mock_insert.call_args.kwargs
        assert "file_path" in call_kwargs, "lightrag_insert should receive file_path"

    def test_lightrag_field_in_result(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        doc = tmp_path / "source" / "lightrag_field.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Check lightrag field.", encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        assert "lightrag" in result
        assert result["lightrag"] in ("inserted", "skipped")

    def test_lightrag_skipped_when_no_content(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        # Create a binary file that read_file_content cannot parse
        doc = tmp_path / "source" / "binary.dat"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"\x00\x01\x02\x03\xff\xfe")

        with patch("niu_photo_server.read_file_content", return_value=None):
            result = ingest_document(str(doc), category="其他", mode="copy")

        # insert should not be called when there's no content
        mock_insert.assert_not_called()
        assert result.get("lightrag") == "skipped"


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

        result = ingest_document(str(doc), category="其他", mode="copy")

        # Check that registry.get was never called with vector-store or kg-server
        mock_reg, _ = mock_registry
        for call in mock_reg.get.call_args_list:
            tool_name = call[0][0] if call[0] else call.kwargs.get("name", "")
            assert not tool_name.startswith("vector-store/"), \
                f"vector-store tool was called: {tool_name}"
            assert not tool_name.startswith("kg-server/"), \
                f"kg-server tool was called: {tool_name}"

    def test_no_store_document_l1_called(self, mock_registry, mock_preferences, tmp_path):
        """Ensure ingest_document does NOT call store_document_l1 internally."""
        from niu_photo_server import ingest_document

        doc = tmp_path / "source" / "no_l1.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("Content for no L1 test.", encoding="utf-8")

        with patch("niu_photo_server.store_document_l1") as mock_l1:
            result = ingest_document(str(doc), category="其他", mode="copy")
            mock_l1.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Auto-detect file type (directory/photo/document)
# ---------------------------------------------------------------------------


class TestFileTypeDetection:
    """Verify auto-detection of file type."""

    def test_directory_with_photos_delegates_to_batch(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        photo_dir = tmp_path / "photo_dir"
        photo_dir.mkdir()
        (photo_dir / "img1.jpg").write_bytes(b"\xff\xd8\xff\xe0")
        (photo_dir / "img2.png").write_bytes(b"\x89PNG")

        with patch("niu_photo_server.ingest_photos_batch") as mock_batch:
            mock_batch.return_value = {"status": "success", "total": 2}
            result = ingest_document(str(photo_dir), category="其他", mode="copy")
            mock_batch.assert_called_once_with(str(photo_dir), "其他")

    def test_directory_without_photos_returns_error(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        empty_dir = tmp_path / "empty_dir"
        empty_dir.mkdir()
        (empty_dir / "notes.txt").write_text("Just a text file.")

        result = ingest_document(str(empty_dir), category="其他", mode="copy")
        assert result["status"] == "error"
        assert result["error_code"] == "DIRECTORY_NO_PHOTOS"

    def test_photo_file_delegates_to_ingest_photo(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0fake_jpg_data")

        with patch("niu_photo_server.ingest_photo") as mock_photo:
            mock_photo.return_value = {"status": "success", "action": "created"}
            result = ingest_document(str(photo), category="其他", mode="copy")
            mock_photo.assert_called_once_with(str(photo), "其他", "copy")

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
# 5. Full-text to ainsert, no truncation for LightRAG
# ---------------------------------------------------------------------------


class TestNoTruncation:
    """Verify that content is NOT truncated when sent to LightRAG."""

    def test_long_content_not_truncated(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        mock_reg, mock_insert = mock_registry

        # Create content longer than the old 10000-char truncation limit
        long_content = "X" * 15000
        doc = tmp_path / "source" / "long_doc.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(long_content, encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        mock_insert.assert_called_once()
        call_kwargs = mock_insert.call_args.kwargs
        passed_content = call_kwargs.get("content")
        assert passed_content is not None
        assert len(passed_content) == 15000, (
            f"Full content should be passed to LightRAG without truncation, "
            f"got {len(passed_content)} chars instead of 15000"
        )

    def test_content_length_reflects_full_size(self, mock_registry, mock_preferences, tmp_path):
        from niu_photo_server import ingest_document

        content = "Hello world! " * 500  # ~6500 chars
        doc = tmp_path / "source" / "size_check.txt"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text(content, encoding="utf-8")

        result = ingest_document(str(doc), category="其他", mode="copy")

        assert result["content_length"] == len(content), (
            f"content_length should reflect full content size, "
            f"got {result['content_length']} instead of {len(content)}"
        )


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
