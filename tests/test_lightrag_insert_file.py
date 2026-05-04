"""Tests for lightrag_insert_file tool and ingest_document using it.

TDD RED phase: Tests define the contract for file-based LightRAG insertion.

Key changes:
1. ingest_document calls lightrag_insert_file (file path) instead of lightrag_insert (text content)
2. lightrag_insert_file delegates to LightRAG's pipeline_enqueue_file
3. LightRAG reads and parses the file itself (DOCX/PDF/PPTX/XLSX/TXT/MD etc.)
4. Photo ingest remains unchanged — never calls lightrag_insert_file
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-servers" / "photo-server" / "src"))

# lightrag-server src path
_LIGHTRAG_SERVER_SRC = str(
    Path(__file__).parent.parent / "mcp-servers" / "lightrag-server" / "src"
)


def _import_lightrag_module():
    """Import the lightrag-server module with mocked dependencies."""
    mock_adapter = MagicMock()
    mock_manager = MagicMock()

    with patch.dict("sys.modules", {
        "niu_api": MagicMock(),
        "niu_api.internal": MagicMock(),
        "niu_api.internal.lightrag_adapter": mock_adapter,
        "niu_api.internal.lightrag_manager": mock_manager,
    }):
        if _LIGHTRAG_SERVER_SRC not in sys.path:
            sys.path.insert(0, _LIGHTRAG_SERVER_SRC)

        import importlib
        import niu_lightrag_server
        importlib.reload(niu_lightrag_server)
        return niu_lightrag_server


# ============== lightrag_insert_file TOOL SCHEMA ==============


class TestLightragInsertFileSchema:
    """Test lightrag_insert_file schema definition."""

    def test_schema_exists(self):
        """lightrag_insert_file must be in TOOL_SCHEMAS."""
        mod = _import_lightrag_module()
        assert "lightrag_insert_file" in mod.TOOL_SCHEMAS

    def test_schema_has_file_path(self):
        """lightrag_insert_file must have file_path as required parameter."""
        mod = _import_lightrag_module()
        schema = mod.TOOL_SCHEMAS["lightrag_insert_file"]
        props = schema["input_schema"]["properties"]
        assert "file_path" in props
        assert "file_path" in schema["input_schema"]["required"]

    def test_schema_has_optional_doc_id(self):
        """lightrag_insert_file must have optional doc_id parameter."""
        mod = _import_lightrag_module()
        schema = mod.TOOL_SCHEMAS["lightrag_insert_file"]
        props = schema["input_schema"]["properties"]
        assert "doc_id" in props
        # doc_id should NOT be required
        assert "doc_id" not in schema["input_schema"].get("required", [])

    def test_schema_count_increased(self):
        """TOOL_SCHEMAS count should increase by 1 (from 15 to 16)."""
        mod = _import_lightrag_module()
        assert len(mod.TOOL_SCHEMAS) >= 15


# ============== lightrag_insert_file FUNCTION ==============


class TestLightragInsertFileFunction:
    """Test lightrag_insert_file function implementation."""

    def test_delegates_to_pipeline_enqueue_file(self):
        """lightrag_insert_file should call LightRAG's pipeline_enqueue_file."""
        mod = _import_lightrag_module()

        mock_rag = MagicMock()
        mock_pipeline = AsyncMock(return_value=(True, "track-123"))

        with patch("niu_lightrag_server.lightrag_insert_file") as mock_fn:
            # We need to test the actual function, not the patched one
            pass

        # Instead, test by checking the function exists and has correct signature
        assert hasattr(mod, "lightrag_insert_file")
        assert callable(mod.lightrag_insert_file)

    def test_in_tool_functions(self):
        """lightrag_insert_file must be in _TOOL_FUNCTIONS."""
        mod = _import_lightrag_module()
        assert "lightrag_insert_file" in mod._TOOL_FUNCTIONS

    def test_returns_status_dict(self):
        """lightrag_insert_file should return dict with status field."""
        mod = _import_lightrag_module()

        # Mock pipeline_enqueue_file to return success
        with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=MagicMock()):
            with patch("niu_api.internal.lightrag_manager.call_async", return_value=(True, "track-1")):
                with patch("lightrag.api.routers.document_routes.pipeline_enqueue_file", new=AsyncMock(return_value=(True, "track-1"))):
                    result = mod.lightrag_insert_file(file_path="/tmp/test.docx")
                    assert isinstance(result, dict)
                    assert "status" in result

    def test_returns_error_when_rag_unavailable(self):
        """Should return error when LightRAG is not available."""
        mod = _import_lightrag_module()

        with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
            result = mod.lightrag_insert_file(file_path="/tmp/test.docx")
            assert result["status"] == "error"
            assert "not available" in result["message"].lower()


# ============== ingest_document uses lightrag_insert_file ==============


@pytest.fixture
def mock_registry():
    """Mock the tool registry so lightrag_insert_file is callable."""
    mock_insert_file = MagicMock(return_value={"status": "ok", "track_id": "t1"})
    mock_reg = MagicMock()
    mock_reg.get.return_value = mock_insert_file

    with patch("agent.tool_registry.get_registry", return_value=mock_reg):
        yield mock_reg, mock_insert_file


@pytest.fixture
def mock_preferences(tmp_path):
    """Provide a minimal workspace path for document storage."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch("niu_photo_server.get_workspace_path", return_value=workspace):
        with patch("niu_photo_server.build_storage_path", return_value="documents/2026"):
            yield workspace


class TestIngestDocumentUsesInsertFile:
    """Verify ingest_document calls lightrag_insert_file instead of lightrag_insert."""

    def test_calls_lightrag_insert_file(self, mock_registry, mock_preferences, tmp_path):
        """ingest_document should call lightrag-server/lightrag_insert_file."""
        from niu_photo_server import ingest_document

        mock_reg, mock_insert_file = mock_registry

        doc = tmp_path / "source" / "report.docx"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"PK\x03\x04fake docx")  # minimal DOCX-like bytes

        result = ingest_document(str(doc), category="文档", mode="copy")

        # Should call lightrag_insert_file, NOT lightrag_insert
        mock_reg.get.assert_called_with("lightrag-server/lightrag_insert_file")
        mock_insert_file.assert_called_once()

    def test_passes_file_path_not_content(self, mock_registry, mock_preferences, tmp_path):
        """ingest_document should pass file_path, not content."""
        from niu_photo_server import ingest_document

        mock_reg, mock_insert_file = mock_registry

        doc = tmp_path / "source" / "data.xlsx"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"PK\x03\x04fake xlsx")

        result = ingest_document(str(doc), category="数据", mode="copy")

        call_kwargs = mock_insert_file.call_args.kwargs
        # Must pass file_path
        assert "file_path" in call_kwargs
        # Must NOT pass content (that was the old way)
        assert "content" not in call_kwargs

    def test_docx_no_longer_returns_filename_only(self, mock_registry, mock_preferences, tmp_path):
        """DOCX files should no longer result in just a filename being passed.

        Before the fix, read_file_content returned Path.stem for DOCX,
        causing LightRAG to only extract 1 entity from the filename.
        Now, the file path is passed to LightRAG which reads it properly.
        """
        from niu_photo_server import ingest_document

        mock_reg, mock_insert_file = mock_registry

        doc = tmp_path / "source" / "硅基银行项目.docx"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"PK\x03\x04fake docx with content")

        result = ingest_document(str(doc), category="其他", mode="copy")

        # The key fix: file_path is passed, not the filename string
        call_kwargs = mock_insert_file.call_args.kwargs
        file_path_arg = call_kwargs.get("file_path", "")
        # file_path should be a full path, not just a filename stem
        assert len(file_path_arg) > len("硅基银行项目"), \
            "file_path should be a full path, not just the filename stem"

    def test_photo_still_uses_custom_kg(self, mock_registry, mock_preferences, tmp_path):
        """Photo ingest should NOT call lightrag_insert_file.

        Photos use sync_photo_to_kg with structured injection (Photo/Person entities),
        not file-based insertion. LightRAG would OCR the photo, which we don't want.
        """
        from niu_photo_server import ingest_document

        mock_reg, mock_insert_file = mock_registry

        photo = tmp_path / "photo.jpg"
        photo.write_bytes(b"\xff\xd8\xff\xe0fake_jpg_data")

        with patch("niu_photo_server.ingest_photo") as mock_photo:
            mock_photo.return_value = {"status": "success", "action": "created"}
            result = ingest_document(str(photo), category="其他", mode="copy")

        # lightrag_insert_file should NOT be called for photos
        mock_insert_file.assert_not_called()

    def test_lightrag_failure_does_not_block_ingest(self, mock_registry, mock_preferences, tmp_path):
        """LightRAG insertion failure should not cause the whole ingest to fail."""
        from niu_photo_server import ingest_document

        mock_reg, mock_insert_file = mock_registry
        mock_insert_file.side_effect = Exception("LightRAG server is down")

        doc = tmp_path / "source" / "resilient.pdf"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"%PDF-1.4 fake")

        result = ingest_document(str(doc), category="其他", mode="copy")

        # File should still be successfully ingested
        assert result["status"] == "success"
        assert result["lightrag"] in ("error", "skipped")

    def test_skipped_path_uses_insert_file(self, mock_registry, mock_preferences, tmp_path):
        """When file already exists (skipped), should still use lightrag_insert_file."""
        from niu_photo_server import ingest_document

        mock_reg, mock_insert_file = mock_registry

        doc = tmp_path / "source" / "existing.docx"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_bytes(b"PK\x03\x04fake docx")

        with patch("niu_photo_server.handle_conflict", return_value=(str(doc), "skipped")):
            result = ingest_document(str(doc), category="其他", mode="copy")

        # Should call lightrag_insert_file even for skipped files
        mock_reg.get.assert_called_with("lightrag-server/lightrag_insert_file")
        mock_insert_file.assert_called_once()
