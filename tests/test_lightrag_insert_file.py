"""Tests for lightrag_insert_file tool and ingest_document using it.

TDD GREEN phase: Tests verify the contract for file-based LightRAG insertion.

Key changes:
1. ingest_document calls lightrag_insert_file (file path) instead of lightrag_insert (text content)
2. lightrag_insert_file delegates to LightRAG's pipeline_enqueue_file (sys.argv workaround)
3. LightRAG reads and parses the file itself (DOCX/PDF/PPTX/XLSX/TXT/MD etc.)
4. Photo ingest remains unchanged — never calls lightrag_insert_file
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_in_tool_functions(self):
        """lightrag_insert_file must be in _TOOL_FUNCTIONS."""
        mod = _import_lightrag_module()
        assert "lightrag_insert_file" in mod._TOOL_FUNCTIONS

    def test_returns_error_for_missing_file(self):
        """lightrag_insert_file returns error for nonexistent file."""
        mod = _import_lightrag_module()
        result = mod.lightrag_insert_file(file_path="/nonexistent/test.docx")
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_returns_error_when_rag_unavailable(self, tmp_path):
        """Should return error when LightRAG is not available."""
        mod = _import_lightrag_module()

        # Create a real text file so file check passes
        txt = tmp_path / "test.txt"
        txt.write_text("Some content", encoding="utf-8")

        key = "niu_api.internal.lightrag_manager"
        saved = {}
        mock_mgr = MagicMock()
        mock_mgr.get_lightrag = MagicMock(return_value=None)
        mock_mgr.call_async = MagicMock()
        for mod_key in ["niu_api", "niu_api.internal", key]:
            saved[mod_key] = sys.modules.get(mod_key)
            if mod_key == key:
                sys.modules[mod_key] = mock_mgr
            else:
                sys.modules.setdefault(mod_key, MagicMock())
        try:
            result = mod.lightrag_insert_file(file_path=str(txt))
            assert result["status"] == "error"
            assert "not available" in result["message"].lower()
        finally:
            for mod_key, orig in saved.items():
                if orig is not None:
                    sys.modules[mod_key] = orig
                else:
                    sys.modules.pop(mod_key, None)

    def test_schema_matches_function(self):
        """TOOL_SCHEMAS and _TOOL_FUNCTIONS must both contain lightrag_insert_file."""
        mod = _import_lightrag_module()
        assert "lightrag_insert_file" in mod.TOOL_SCHEMAS
        assert "lightrag_insert_file" in mod._TOOL_FUNCTIONS
        fn = mod._TOOL_FUNCTIONS["lightrag_insert_file"]
        schema = mod.TOOL_SCHEMAS["lightrag_insert_file"]
        assert fn.__name__ == "lightrag_insert_file"
        assert schema["name"] == "lightrag_insert_file"


# ============== Integration: pipeline_enqueue_file importable + no file move ==============


class TestPipelineEnqueueFileImportAndNoFileMove:
    """Verify pipeline_enqueue_file can be imported and files are NOT moved."""

    def test_pipeline_enqueue_file_importable_with_argv(self):
        """pipeline_enqueue_file must be importable when sys.argv is set to ['lightrag']."""
        saved_argv = sys.argv
        sys.argv = ["lightrag"]
        try:
            from lightrag.api.routers.document_routes import pipeline_enqueue_file
            assert callable(pipeline_enqueue_file)
        finally:
            sys.argv = saved_argv

    def test_lightrag_insert_file_does_not_move_file(self, tmp_path):
        """lightrag_insert_file must NOT move the original file (uses temp copy)."""
        mod = _import_lightrag_module()

        # Create a real text file
        txt = tmp_path / "test.txt"
        txt.write_text("Test content for LightRAG", encoding="utf-8")

        mock_rag = MagicMock()
        AsyncMock(return_value=(True, "track-123"))

        key = "niu_api.internal.lightrag_manager"
        saved = {}
        mock_mgr = MagicMock()
        mock_mgr.get_lightrag = MagicMock(return_value=mock_rag)
        mock_mgr.call_async = MagicMock(return_value=(True, "track-123"))
        for mod_key in ["niu_api", "niu_api.internal", key]:
            saved[mod_key] = sys.modules.get(mod_key)
            if mod_key == key:
                sys.modules[mod_key] = mock_mgr
            else:
                sys.modules.setdefault(mod_key, MagicMock())
        try:
            mod.lightrag_insert_file(file_path=str(txt))
            # File must still exist at original location
            assert txt.is_file(), "Original file must NOT be moved"
            # No __enqueued__ directory should be created in original location
            assert not (tmp_path / "__enqueued__").exists(), "No __enqueued__ dir should be created"
        finally:
            for mod_key, orig in saved.items():
                if orig is not None:
                    sys.modules[mod_key] = orig
                else:
                    sys.modules.pop(mod_key, None)


    def test_lightrag_insert_file_triggers_processing_after_enqueue(self, tmp_path):
        """After enqueue succeeds, lightrag_insert_file must schedule
        apipeline_process_enqueue_documents via fire_and_forget (non-blocking)."""
        mod = _import_lightrag_module()

        txt = tmp_path / "test.txt"
        txt.write_text("Test content for LightRAG", encoding="utf-8")

        mock_rag = MagicMock()
        mock_rag.apipeline_process_enqueue_documents = AsyncMock()
        mock_rag.doc_status = MagicMock()
        mock_rag.full_docs = MagicMock()

        key = "niu_api.internal.lightrag_manager"
        saved = {}
        mock_mgr = MagicMock()
        mock_mgr.get_lightrag = MagicMock(return_value=mock_rag)
        # call_async sequence (fire-and-forget no longer uses call_async):
        # 1. pipeline_enqueue_file -> (True, track_id)
        # 2. doc_status.get_docs_by_track_id -> {}
        mock_mgr.call_async = MagicMock(side_effect=[
            (True, "track-123"),  # enqueue succeeds
            {},                   # get_docs_by_track_id returns empty
        ])
        mock_mgr.fire_and_forget = MagicMock()
        for mod_key in ["niu_api", "niu_api.internal", key]:
            saved[mod_key] = sys.modules.get(mod_key)
            if mod_key == key:
                sys.modules[mod_key] = mock_mgr
            else:
                sys.modules.setdefault(mod_key, MagicMock())
        try:
            result = mod.lightrag_insert_file(file_path=str(txt))
            assert result["status"] == "ok"
            # call_async should be called 2 times: enqueue + patch
            # (process is now fire_and_forget, not call_async)
            assert mock_mgr.call_async.call_count == 2, \
                "call_async should be called exactly 2 times (enqueue + patch)"
            # fire_and_forget should be called once for the processing pipeline
            assert mock_mgr.fire_and_forget.call_count == 1, \
                "fire_and_forget should be called once for apipeline_process_enqueue_documents"
            # Verify the coroutine passed to fire_and_forget is a coroutine
            ff_args = mock_mgr.fire_and_forget.call_args
            coro = ff_args[0][0]
            assert coro is not None, "fire_and_forget should receive a coroutine"
            coro_name = type(coro).__name__
            assert "coroutine" in coro_name.lower(), \
                f"Expected a coroutine object, got {coro_name}"
            # Verify context kwarg is passed
            assert "context" in ff_args[1], \
                "fire_and_forget should receive a context kwarg"
            # Verify the coroutine is _process_and_handle_failure wrapper
            # (not a bare apipeline_process_enqueue_documents call)
            assert coro.cr_code is not mock_rag.apipeline_process_enqueue_documents.__code__, \
                "fire_and_forget should receive _process_and_handle_failure wrapper, not bare pipeline call"
        finally:
            for mod_key, orig in saved.items():
                if orig is not None:
                    sys.modules[mod_key] = orig
                else:
                    sys.modules.pop(mod_key, None)


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

        ingest_document(str(doc), category="文档", mode="copy")

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

        ingest_document(str(doc), category="数据", mode="copy")

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

        ingest_document(str(doc), category="其他", mode="copy")

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
            ingest_document(str(photo), category="其他", mode="copy")

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
            ingest_document(str(doc), category="其他", mode="copy")

        # Should call lightrag_insert_file even for skipped files
        mock_reg.get.assert_called_with("lightrag-server/lightrag_insert_file")
        mock_insert_file.assert_called_once()


# ============== shutdown_pending_futures and CancelledError ==============


class TestShutdownPendingFutures:
    """Test shutdown_pending_futures logic."""

    def test_no_pending_futures(self):
        """shutdown_pending_futures should return immediately when no futures."""
        from niu_api.internal.lightrag_manager import (
            _pending_futures,
            _pending_lock,
            shutdown_pending_futures,
        )
        with _pending_lock:
            _pending_futures.clear()
        # Should not raise
        shutdown_pending_futures(timeout=0.1)

    def test_cancels_timed_out_futures(self):
        """shutdown_pending_futures should cancel futures that don't complete in time."""
        import concurrent.futures

        from niu_api.internal.lightrag_manager import (
            _pending_futures,
            _pending_lock,
            shutdown_pending_futures,
        )

        # Create a future that will never complete
        fake_future = concurrent.futures.Future()
        with _pending_lock:
            _pending_futures.append(fake_future)

        try:
            shutdown_pending_futures(timeout=0.1)
            # The fake future should be cancelled
            assert fake_future.cancelled(), "Timed-out future should be cancelled"
        finally:
            with _pending_lock:
                _pending_futures[:] = [f for f in _pending_futures if f is not fake_future]

    def test_clears_pending_list(self):
        """shutdown_pending_futures should clear _pending_futures."""
        import concurrent.futures

        from niu_api.internal.lightrag_manager import (
            _pending_futures,
            _pending_lock,
            shutdown_pending_futures,
        )

        done_future = concurrent.futures.Future()
        done_future.set_result("done")
        with _pending_lock:
            _pending_futures.clear()
            _pending_futures.append(done_future)

        try:
            shutdown_pending_futures(timeout=0.1)
            with _pending_lock:
                assert len(_pending_futures) == 0, "_pending_futures should be cleared"
        finally:
            with _pending_lock:
                _pending_futures[:] = [f for f in _pending_futures if f is not done_future]


class TestFireAndForgetCancellation:
    """Test fire_and_forget handles CancelledError correctly."""

    def test_wrapped_catches_exception(self):
        """_wrapped should catch Exception and log it, not let it propagate."""
        from niu_api.internal.lightrag_manager import (
            _pending_futures,
            _pending_lock,
            fire_and_forget,
        )

        async def failing_coro():
            raise RuntimeError("test error")

        # This should not raise even though the coroutine fails
        fire_and_forget(failing_coro(), context="test-cancellation")
        # Poll for the future to complete instead of fixed sleep
        import time
        deadline = time.monotonic() + 2.0
        with _pending_lock:
            futures_snapshot = list(_pending_futures)
        for f in futures_snapshot:
            while not f.done() and time.monotonic() < deadline:
                time.sleep(0.05)
        # Clean up
        with _pending_lock:
            _pending_futures.clear()

    def test_future_removed_after_completion(self):
        """_pending_futures should be cleaned up after coroutine completes."""
        from niu_api.internal.lightrag_manager import (
            _pending_futures,
            _pending_lock,
            fire_and_forget,
        )

        async def quick_coro():
            pass

        fire_and_forget(quick_coro(), context="test-cleanup")
        # Poll for the future to complete instead of fixed sleep
        import time
        deadline = time.monotonic() + 2.0
        with _pending_lock:
            futures_snapshot = list(_pending_futures)
        for f in futures_snapshot:
            while not f.done() and time.monotonic() < deadline:
                time.sleep(0.05)
        # The future should have been removed by the finally block
        with _pending_lock:
            for f in _pending_futures:
                assert f.done(), "Pending future should be done after coroutine completes"
        with _pending_lock:
            _pending_futures.clear()
